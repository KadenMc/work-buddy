"""Slice C.2: create_task idempotency cache.

Live-test 2026-04-28 morning surfaced a non-idempotency bug:
``create_task`` regenerated ``task_id`` and ``note_uuid`` on every
invocation, so a sidecar retry of a PWU-failed task_create produced a
NEW task_id and note_uuid each attempt — orphan notes piled up in
``tasks/notes/`` with no corresponding master-list line.

Fix: a 5-minute idempotency cache keyed on a hash of natural input
parameters. Same input within the window → same IDs → existing-line
and existing-note guards finally match. Different inputs → fresh IDs.
After the TTL expires, even identical inputs get fresh IDs (so users
can legitimately create the same task name twice on different days).

Tests:
1. Same params within TTL → identical IDs across calls
2. Different params → different IDs
3. TTL expiry → fresh IDs
4. note_uuid only generated when summary is set
5. The existing-note short-circuit prevents duplicate note writes
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from work_buddy.obsidian.tasks import mutations


@pytest.fixture
def isolated_idem_dir(tmp_path: Path, monkeypatch) -> Path:
    """Point the idempotency cache at tmp_path and return its location."""
    cache_dir = tmp_path / "create_task_idempotency"
    monkeypatch.setattr(mutations, "_idempotency_dir", lambda: cache_dir)
    return cache_dir


# ---------------------------------------------------------------------------
# Cache primitives
# ---------------------------------------------------------------------------


def test_cache_round_trip(isolated_idem_dir):
    key = "abc123"
    mutations._record_idempotent_create_ids(key, "t-deadbeef", "uuid-1234")
    task_id, note_uuid = mutations._resolve_idempotent_create_ids(key)
    assert task_id == "t-deadbeef"
    assert note_uuid == "uuid-1234"


def test_cache_no_entry_returns_none(isolated_idem_dir):
    task_id, note_uuid = mutations._resolve_idempotent_create_ids("missing")
    assert task_id is None
    assert note_uuid is None


def test_cache_expired_returns_none(isolated_idem_dir):
    """Entries past the TTL window are ignored."""
    key = "old-entry"
    cache_path = isolated_idem_dir
    cache_path.mkdir(parents=True, exist_ok=True)
    (cache_path / f"{key}.json").write_text(
        json.dumps({
            "task_id": "t-stale",
            "note_uuid": "uuid-stale",
            "ts": time.time() - mutations._IDEMPOTENCY_TTL_SEC - 10,
        }),
        encoding="utf-8",
    )
    task_id, note_uuid = mutations._resolve_idempotent_create_ids(key)
    assert task_id is None
    assert note_uuid is None


def test_cache_malformed_json_returns_none(isolated_idem_dir):
    """A corrupt cache file should be treated as a miss, not crash."""
    isolated_idem_dir.mkdir(parents=True, exist_ok=True)
    (isolated_idem_dir / "malformed.json").write_text(
        "not valid json {{",
        encoding="utf-8",
    )
    task_id, note_uuid = mutations._resolve_idempotent_create_ids("malformed")
    assert task_id is None
    assert note_uuid is None


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_key_stable_across_identical_inputs():
    k1 = mutations._create_task_idempotency_key(
        task_text="Fix bug",
        summary="long summary",
        project="work-buddy",
        urgency="high",
        contract=None,
        tags=["paper/ecg", "experiment/aug"],
        due_date=None,
    )
    k2 = mutations._create_task_idempotency_key(
        task_text="Fix bug",
        summary="long summary",
        project="work-buddy",
        urgency="high",
        contract=None,
        tags=["experiment/aug", "paper/ecg"],  # different order — sorted before hash
        due_date=None,
    )
    assert k1 == k2


def test_key_differs_for_different_text():
    k1 = mutations._create_task_idempotency_key(
        task_text="Fix bug A",
        summary=None, project=None, urgency="medium",
        contract=None, tags=[], due_date=None,
    )
    k2 = mutations._create_task_idempotency_key(
        task_text="Fix bug B",
        summary=None, project=None, urgency="medium",
        contract=None, tags=[], due_date=None,
    )
    assert k1 != k2


def test_key_differs_for_different_summary():
    k1 = mutations._create_task_idempotency_key(
        task_text="task", summary="A",
        project=None, urgency="medium", contract=None, tags=[], due_date=None,
    )
    k2 = mutations._create_task_idempotency_key(
        task_text="task", summary="B",
        project=None, urgency="medium", contract=None, tags=[], due_date=None,
    )
    assert k1 != k2


# ---------------------------------------------------------------------------
# create_task end-to-end with cache
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_bridge_store(isolated_idem_dir):
    """Bridge / store mocks plus consent bypass."""
    with patch.object(mutations, "bridge") as mock_bridge, \
         patch.object(mutations, "store") as mock_store, \
         patch("work_buddy.consent._cache") as mock_cache:
        mock_cache.is_granted.return_value = True
        mock_cache.get_mode.return_value = "always"
        mock_bridge.write_file.return_value = True
        # read_file returns None → simulates "note doesn't exist yet"
        # for both note_path and template lookups; for the master list,
        # tests override per-call.
        mock_bridge.read_file.return_value = None
        mock_bridge.atomic_replace_line_by_task_id.return_value = {
            "error": "bridge_returned_none",
        }
        # store.get returns None → "no record yet" so create proceeds.
        mock_store.get.return_value = None
        mock_store.create.return_value = {"task_id": "ignored"}
        # store validators must pass through.
        mock_store.VALID_URGENCIES = {"low", "medium", "high"}
        mock_store.VALID_TASK_KINDS = {"task", "periodic", "habit"}
        mock_store.VALID_DENSITIES = {"sparse", "developed", "dense"}
        mock_store.VALID_CREATION_EFFORTS = {"sparse", "medium", "developed"}
        mock_store.VALID_USER_INVOLVEMENTS = {"low", "medium", "high"}
        yield mock_bridge, mock_store


def _master_list_reads(*reads):
    """Build a side_effect that returns master-list content on each
    bridge.read_file call. Other reads return None."""
    def _factory(template_misses=False):
        idx = [0]
        def side(path):
            # Master list reads happen at known points; everything else
            # (template, note path) returns None.
            if path == mutations.MASTER_TASK_FILE:
                if idx[0] < len(reads):
                    val = reads[idx[0]]
                    idx[0] += 1
                    return val
                return reads[-1] if reads else None
            return None
        return side
    return _factory()


def test_create_task_caches_ids_on_first_call(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.read_file.side_effect = _master_list_reads(
        "# Empty list\n",
    )

    result = mutations.create_task(
        task_text="Cache test 1",
        urgency="medium",
    )
    assert result["success"]
    task_id_1 = result["task_id"]

    # Second call with identical params → SAME task_id (from cache).
    mock_bridge.read_file.side_effect = _master_list_reads(
        "# Empty list\n",
    )
    result2 = mutations.create_task(
        task_text="Cache test 1",
        urgency="medium",
    )
    assert result2["task_id"] == task_id_1


def test_create_task_different_text_yields_different_task_id(
    patched_bridge_store,
):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.read_file.side_effect = _master_list_reads(
        "# Empty list\n",
    )
    r1 = mutations.create_task(task_text="Alpha", urgency="medium")

    mock_bridge.read_file.side_effect = _master_list_reads(
        "# Empty list\n",
    )
    r2 = mutations.create_task(task_text="Beta", urgency="medium")

    assert r1["task_id"] != r2["task_id"]


def test_create_task_with_summary_caches_note_uuid(patched_bridge_store):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.read_file.side_effect = _master_list_reads(
        "# Empty list\n",
    )

    r1 = mutations.create_task(
        task_text="Note caching",
        summary="A linked note for retry tests",
        urgency="medium",
    )
    note_uuid_1 = r1["note_uuid"]
    assert note_uuid_1 is not None

    # Second call with identical params reuses the same note_uuid.
    mock_bridge.read_file.side_effect = _master_list_reads(
        "# Empty list\n",
    )
    r2 = mutations.create_task(
        task_text="Note caching",
        summary="A linked note for retry tests",
        urgency="medium",
    )
    assert r2["note_uuid"] == note_uuid_1


def test_create_task_skips_note_write_when_already_present(
    patched_bridge_store,
):
    """The retry-safety check: if the note path returns content (existing
    file from a prior PWU attempt), don't rewrite it."""
    mock_bridge, _ = patched_bridge_store

    note_already_landed = (
        "---\ntype: task-note\ncreated: 2026-04-28\nstatus: open\n---\n"
        "# Existing\n## Summary\nleft over from prior PWU\n"
    )

    def selective_read(path):
        if path == mutations.MASTER_TASK_FILE:
            return "# Empty list\n"
        if path.startswith("tasks/notes/"):
            return note_already_landed
        return None

    mock_bridge.read_file.side_effect = selective_read

    result = mutations.create_task(
        task_text="Skip-rewrite-when-present",
        summary="The note already exists from a PWU'd previous attempt",
        urgency="medium",
    )
    assert result["success"]

    # Confirm: no write was made to the notes/ path. The only write_file
    # call should be for the master-list (one call).
    write_paths = [c.args[0] for c in mock_bridge.write_file.call_args_list]
    note_writes = [p for p in write_paths if p.startswith("tasks/notes/")]
    assert note_writes == [], (
        f"Note should not have been rewritten, but write_file was called "
        f"with: {note_writes}"
    )
