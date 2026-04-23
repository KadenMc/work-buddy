"""Unit tests for EditorConflict + write_file_raw behavior.

Covers:
- EditorConflict is classified as transient (so the retry queue picks it up)
- write_file_raw raises EditorConflict immediately on 409 (no in-bridge retry —
  retries belong above the read-modify-write layer so each attempt re-reads)
- write_file_raw returns False (not raises) on non-409 failures
- vault_write does NOT fall back on EditorConflict
"""
from __future__ import annotations

from unittest.mock import patch
import pytest

from work_buddy.errors import classify_error
from work_buddy.obsidian.bridge import EditorConflict, write_file_raw


class TestEditorConflictClassification:
    def test_editor_conflict_is_transient(self):
        """The retry queue's auto-enqueue depends on this."""
        exc = EditorConflict("foo/bar.md")
        assert classify_error(exc) == "transient"

    def test_editor_conflict_message_pattern_also_caught(self):
        """Defense-in-depth: even a generic exception with the marker
        in its message should classify as transient."""
        exc = RuntimeError("editor_dirty: tasks/master-task-list.md")
        assert classify_error(exc) == "transient"


class TestWriteFileRawBehavior:
    """Mock _request_with_status to drive different bridge responses."""

    def _patch_status(self, *responses):
        """Return a mock that yields successive (status, body) tuples."""
        return patch(
            "work_buddy.obsidian.bridge._request_with_status",
            side_effect=list(responses),
        )

    def test_success_first_attempt(self):
        with self._patch_status((200, {"path": "x.md", "created": False})):
            assert write_file_raw("x.md", "content") is True

    def test_raises_editor_conflict_immediately_on_409(self):
        """No in-bridge retry — gateway/sidecar handle re-invocation
        from scratch so each attempt re-reads the source file."""
        with patch("work_buddy.obsidian.bridge.time.sleep") as mock_sleep, \
             self._patch_status((409, {"error": "editor_dirty"})):
            with pytest.raises(EditorConflict) as excinfo:
                write_file_raw("x.md", "content")
            assert excinfo.value.path == "x.md"
            # No sleep should have been called — we don't wait, we surface.
            mock_sleep.assert_not_called()

    def test_network_failure_returns_false(self):
        """Bridge down → status is None → return False so the existing
        higher-layer fallback (vault_write's direct-fs path) can run."""
        with self._patch_status((None, None)):
            assert write_file_raw("x.md", "content") is False

    def test_other_4xx_returns_false(self):
        """A 400 / 404 / 500 isn't a transient conflict — don't raise
        EditorConflict, return False so callers don't think they have
        a recoverable conflict."""
        with self._patch_status((400, {"error": "bad request"})):
            assert write_file_raw("x.md", "content") is False


class TestVaultWriteDoesNotFallBackOnConflict:
    """The whole point of the conflict signal is to NOT clobber the editor.
    A direct-fs fallback would still get clobbered when the user saves."""

    def test_editor_conflict_propagates_through_vault_write(self):
        from work_buddy.obsidian import vault_writer
        from pathlib import Path

        with patch(
            "work_buddy.obsidian.bridge.is_available", return_value=True
        ), patch(
            "work_buddy.obsidian.bridge.write_file_raw",
            side_effect=EditorConflict("x.md"),
        ):
            with pytest.raises(EditorConflict):
                vault_writer.vault_write(
                    "x.md", Path("/tmp/x.md"), "new content"
                )
