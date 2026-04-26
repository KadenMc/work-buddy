"""Unit tests for EditorConflict + write_file_raw behavior.

Covers:
- ObsidianEditorConflict (legacy alias: EditorConflict) is classified as transient
- write_file_raw raises ObsidianEditorConflict immediately on 409
- write_file_raw raises ObsidianPostWriteUncertain on PUT timeout (port open)
  to close the latent double-write hazard (CP2)
- write_file_raw returns False on bridge unreachable / 4xx-other-than-409 / 5xx
  (transitional shim for legacy TRANSLATE-pattern callers; CP6 unwraps)
- vault_write does NOT fall back on EditorConflict

After CP2 the mock pattern shifted: ``_request_with_status`` raises
typed exceptions instead of returning ``(None, None)`` for failure, so
mocks use ``side_effect=<exception>`` for failure cases and
``return_value=(status, body)`` for success.
"""
from __future__ import annotations

from unittest.mock import patch
import pytest

from work_buddy.errors import classify_error
from work_buddy.obsidian.bridge import EditorConflict, write_file_raw
from work_buddy.obsidian.errors import (
    ObsidianEditorConflict,
    ObsidianNotRunning,
    ObsidianPostWriteUncertain,
    ObsidianRefused,
    ObsidianServerError,
    ObsidianTimeout,
)


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
    """Mock _request_with_status to drive different bridge responses.

    Post-CP2: success returns ``(status, body)`` from the mock;
    failures raise typed exceptions via ``side_effect=<exception>``.
    """

    def _patch_status_returns(self, status, body):
        """Mock _request_with_status to return a successful response."""
        return patch(
            "work_buddy.obsidian.bridge._request_with_status",
            return_value=(status, body),
        )

    def _patch_status_raises(self, exc):
        """Mock _request_with_status to raise a typed exception."""
        return patch(
            "work_buddy.obsidian.bridge._request_with_status",
            side_effect=exc,
        )

    def test_success_first_attempt(self):
        with self._patch_status_returns(200, {"path": "x.md", "created": False}):
            assert write_file_raw("x.md", "content") is True

    def test_raises_editor_conflict_immediately_on_409(self):
        """No in-bridge retry — gateway/sidecar handle re-invocation
        from scratch so each attempt re-reads the source file."""
        with patch("work_buddy.obsidian.bridge.time.sleep") as mock_sleep, \
             self._patch_status_raises(ObsidianEditorConflict("x.md")):
            with pytest.raises(EditorConflict) as excinfo:
                write_file_raw("x.md", "content")
            assert excinfo.value.path == "x.md"
            # No sleep should have been called — we don't wait, we surface.
            mock_sleep.assert_not_called()

    def test_legacy_alias_catches_typed_instance(self):
        """``except EditorConflict`` must continue catching
        ObsidianEditorConflict instances raised internally — the alias
        keeps un-migrated callers working through the transition."""
        with self._patch_status_raises(ObsidianEditorConflict("x.md")):
            try:
                write_file_raw("x.md", "content")
            except EditorConflict:
                pass  # caught via the alias — good
            else:
                pytest.fail("EditorConflict alias did not catch ObsidianEditorConflict")

    def test_unreachable_returns_false(self):
        """Bridge unreachable (port refused) → write definitely did NOT
        happen. Safe to return False so the existing higher-layer
        fallback (vault_write's direct-fs path) can run.

        Transitional shim — CP6 changes this to re-raise typed."""
        with self._patch_status_raises(ObsidianNotRunning()):
            assert write_file_raw("x.md", "content") is False

    def test_post_write_timeout_raises_uncertain(self):
        """Bridge timeout AFTER PUT body sent → vault state may or may
        not reflect the change. Raise ObsidianPostWriteUncertain so the
        gateway-side verifier can decide. This closes the double-write
        hazard noted in op_34ab708a (CP2)."""
        with self._patch_status_raises(ObsidianTimeout()):
            with pytest.raises(ObsidianPostWriteUncertain) as excinfo:
                write_file_raw("notes/x.md", "hello world")
            assert excinfo.value.path == "notes/x.md"
            assert excinfo.value.write_mode == "replace"
            # Default content_hint for replace mode is sha256 of full content.
            assert excinfo.value.content_hint is not None
            assert excinfo.value.content_hint.startswith("sha256:")

    def test_post_write_uncertain_carries_explicit_hint(self):
        """Caller can override content_hint and write_mode."""
        with self._patch_status_raises(ObsidianTimeout()):
            with pytest.raises(ObsidianPostWriteUncertain) as excinfo:
                write_file_raw(
                    "notes/x.md", "irrelevant",
                    write_mode="insert",
                    content_hint="my unique inserted line",
                )
            assert excinfo.value.write_mode == "insert"
            assert excinfo.value.content_hint == "my unique inserted line"

    def test_other_4xx_returns_false(self):
        """A 400 / 404 isn't a transient conflict — don't raise
        EditorConflict, return False so callers don't think they have
        a recoverable conflict.

        Transitional shim — CP6 changes this to re-raise typed."""
        with self._patch_status_raises(ObsidianRefused(400)):
            assert write_file_raw("x.md", "content") is False

    def test_5xx_returns_false(self):
        """5xx is plugin-side error — return False so the bool
        contract callers see consistent failure shape.

        Transitional shim — CP6 changes this to re-raise typed."""
        with self._patch_status_raises(ObsidianServerError(503)):
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
