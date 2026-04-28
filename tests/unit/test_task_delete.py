"""Slice C.3: delete_task atomic path + bridge_failure on read timeout.

Live-test 2026-04-28 surfaced two bugs in `delete_task`:

1. When `bridge.read_file` returned None (timeout), the function
   returned `{"success": False, "removed": {all-false}}` — a *normal
   dict*, not a `bridge_failure` dict. So `@bridge_retry` didn't see
   the transient marker and never retried. Repeated calls reliably
   failed with all-false on this user's machine.

2. The note-deletion step swallowed every Exception (including typed
   ObsidianError), preventing `@bridge_retry` from catching transient
   bridge failures during note removal.

Fix: use the new atomic line-removal path (mirroring Slice C's atomic
update), fall back to legacy on bridge-down, and on legacy
read-timeout return `bridge_failure` so the retry decorator activates.
For the note step, let typed `ObsidianError` propagate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks import mutations


@pytest.fixture(autouse=True)
def _bypass_consent_and_retry():
    with patch("work_buddy.consent._cache") as mock_cache:
        mock_cache.is_granted.return_value = True
        mock_cache.get_mode.return_value = "always"
        yield


@pytest.fixture
def patched_bridge_store():
    with patch.object(mutations, "bridge") as mock_bridge, \
         patch.object(mutations, "store") as mock_store:
        mock_bridge.read_file.return_value = (
            "- [ ] #todo TestTask 🆔 t-deadbeef\n"
        )
        mock_bridge.write_file.return_value = True
        mock_bridge.eval_js_internal.return_value = "deleted"
        mock_bridge.atomic_delete_line_by_task_id.return_value = {
            "found": True,
            "removed": True,
            "line_number": 1,
            "old_line": "- [ ] #todo TestTask 🆔 t-deadbeef",
        }
        mock_store.get.return_value = {
            "task_id": "t-deadbeef",
            "note_uuid": None,
        }
        mock_store.delete.return_value = True
        yield mock_bridge, mock_store


# ---------------------------------------------------------------------------
# Atomic happy path
# ---------------------------------------------------------------------------


def test_delete_uses_atomic_path(patched_bridge_store):
    mock_bridge, mock_store = patched_bridge_store
    result = mutations.delete_task(task_id="t-deadbeef")

    assert result["success"] is True
    assert result["removed"]["task_line"] is True
    assert result["removed"]["store"] is True
    # Atomic path was called.
    mock_bridge.atomic_delete_line_by_task_id.assert_called_once()
    # Legacy write_file should NOT have been used.
    mock_bridge.write_file.assert_not_called()


def test_delete_atomic_not_found_skips_store(patched_bridge_store):
    mock_bridge, mock_store = patched_bridge_store
    mock_bridge.atomic_delete_line_by_task_id.return_value = {
        "found": False, "removed": False,
    }

    result = mutations.delete_task(task_id="t-deadbeef")
    assert result["success"] is False
    assert result["removed"]["task_line"] is False
    # store.delete should NOT have run because file removal didn't happen.
    mock_store.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Bridge-down → falls back to legacy
# ---------------------------------------------------------------------------


def test_delete_falls_back_to_legacy_on_atomic_obsidian_error(
    patched_bridge_store,
):
    from work_buddy.obsidian.errors import ObsidianUnreachable
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_delete_line_by_task_id.side_effect = ObsidianUnreachable()

    result = mutations.delete_task(task_id="t-deadbeef")

    assert result["success"] is True
    # Legacy bridge.write_file ran.
    mock_bridge.write_file.assert_called_once()


def test_delete_falls_back_to_legacy_on_bridge_returned_none(
    patched_bridge_store,
):
    mock_bridge, _ = patched_bridge_store
    mock_bridge.atomic_delete_line_by_task_id.return_value = {
        "error": "bridge_returned_none",
        "found": False, "removed": False,
    }
    result = mutations.delete_task(task_id="t-deadbeef")
    assert result["success"] is True
    mock_bridge.write_file.assert_called_once()


# ---------------------------------------------------------------------------
# CRITICAL regression test: bridge_failure on legacy read timeout
# ---------------------------------------------------------------------------


def test_delete_returns_bridge_failure_on_legacy_read_timeout(
    patched_bridge_store,
):
    """Pre-fix bug: if the atomic path fell through AND bridge.read_file
    returned None (timeout), the function returned a normal
    success-false dict that @bridge_retry didn't recognize as transient
    → no retry. Reliably failed on slow bridge machines.

    Post-fix: returns bridge_failure dict, which the @bridge_retry
    decorator picks up and retries.
    """
    from work_buddy.obsidian.retry import is_bridge_failure
    from work_buddy.obsidian.errors import ObsidianUnreachable
    mock_bridge, _ = patched_bridge_store
    # Force atomic to fall through:
    mock_bridge.atomic_delete_line_by_task_id.side_effect = ObsidianUnreachable()
    # And the legacy read returns None (timeout):
    mock_bridge.read_file.return_value = None

    # Wrapped function — call .__wrapped__ if needed; bridge_retry will
    # surface the bridge_failure on exhaustion.
    raw_delete = mutations.delete_task.__wrapped__  # peel @bridge_retry
    raw_delete = raw_delete.__wrapped__  # peel @requires_consent
    result = raw_delete(task_id="t-deadbeef")

    assert is_bridge_failure(result), (
        f"Expected bridge_failure dict so @bridge_retry retries; got {result!r}"
    )
    assert "could not read" in result.get("message", "").lower()


# ---------------------------------------------------------------------------
# Note delete: typed ObsidianError propagates (no bare except)
# ---------------------------------------------------------------------------


def test_note_delete_propagates_obsidian_error(patched_bridge_store):
    """Pre-fix bug: bare `except Exception` swallowed ObsidianError,
    blocking the @bridge_retry recovery.

    Post-fix: typed ObsidianError propagates so the retry decorator
    can catch it and retry the whole function."""
    from work_buddy.obsidian.errors import ObsidianTimeout
    mock_bridge, mock_store = patched_bridge_store
    mock_store.get.return_value = {
        "task_id": "t-deadbeef",
        "note_uuid": "abcd-efgh-1234-5678",
    }
    mock_bridge.eval_js_internal.side_effect = ObsidianTimeout()

    raw_delete = mutations.delete_task.__wrapped__  # peel @bridge_retry
    raw_delete = raw_delete.__wrapped__  # peel @requires_consent

    with pytest.raises(ObsidianTimeout):
        raw_delete(task_id="t-deadbeef")


def test_note_delete_runtime_error_does_not_propagate(patched_bridge_store):
    """JS errors inside Obsidian (RuntimeError) are recorded as
    note=False but don't block the rest of the deletion. They're not
    transient bridge failures — they're application-level issues that
    retry won't fix."""
    mock_bridge, mock_store = patched_bridge_store
    mock_store.get.return_value = {
        "task_id": "t-deadbeef",
        "note_uuid": "abcd-efgh-1234-5678",
    }
    mock_bridge.eval_js_internal.side_effect = RuntimeError(
        "Eval error: TypeError: Cannot read property of undefined"
    )

    result = mutations.delete_task(task_id="t-deadbeef")
    # Line was still removed via atomic path; note removal failed but
    # didn't crash the whole function.
    assert result["success"] is True
    assert result["removed"]["task_line"] is True
    assert result["removed"]["note"] is False
