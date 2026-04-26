"""Unit tests for ObsidianEditorConflict + write_file_raw behavior.

Covers:
- ObsidianEditorConflict is classified as transient
- write_file_raw raises ObsidianEditorConflict immediately on 409
- write_file_raw raises ObsidianPostWriteUncertain on PUT timeout (port open)
  to close the latent double-write hazard (CP2/CP5)
- write_file_raw raises typed ObsidianError on bridge unreachable /
  4xx-other-than-409 / 5xx (post-CP6 — no more bool shim)
- vault_write does NOT fall back on ObsidianEditorConflict

After CP2 the mock pattern shifted: ``_request_with_status`` raises
typed exceptions instead of returning ``(None, None)`` for failure, so
mocks use ``side_effect=<exception>`` for failure cases and
``return_value=(status, body)`` for success.

The legacy ``EditorConflict`` alias was removed in CP9. All new code
imports ``ObsidianEditorConflict`` directly from
:mod:`work_buddy.obsidian.errors`.
"""
from __future__ import annotations

from unittest.mock import patch
import pytest

from work_buddy.errors import classify_error
from work_buddy.obsidian.bridge import write_file_raw
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
        exc = ObsidianEditorConflict("foo/bar.md")
        assert classify_error(exc) == "transient"

    def test_editor_conflict_message_format_preserved(self):
        """The legacy 'editor_dirty: <path>' message format survives
        for log scrapers — CP1 preserved this by overriding the
        HTTPError message format in ObsidianEditorConflict.__init__."""
        exc = ObsidianEditorConflict("tasks/master-task-list.md")
        assert "editor_dirty: tasks/master-task-list.md" in str(exc)


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
            with pytest.raises(ObsidianEditorConflict) as excinfo:
                write_file_raw("x.md", "content")
            assert excinfo.value.path == "x.md"
            # No sleep should have been called — we don't wait, we surface.
            mock_sleep.assert_not_called()

    def test_unreachable_raises_typed(self):
        """Post-CP6: bridge unreachable raises ObsidianUnreachable
        (or subclass) directly. Callers either catch typed (e.g.
        vault_write's filesystem fallback) or let the @bridge_retry
        decorator / gateway exception handler classify."""
        with self._patch_status_raises(ObsidianNotRunning()):
            with pytest.raises(ObsidianNotRunning):
                write_file_raw("x.md", "content")

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

    def test_other_4xx_raises_typed(self):
        """Post-CP6: 4xx other than 409 raises ObsidianRefused.
        classify_error returns 'permanent' for this — gateway won't
        enqueue (no retry will help a structural refusal)."""
        with self._patch_status_raises(ObsidianRefused(400)):
            with pytest.raises(ObsidianRefused) as excinfo:
                write_file_raw("x.md", "content")
            assert excinfo.value.status == 400

    def test_5xx_raises_typed(self):
        """Post-CP6: 5xx raises ObsidianServerError.
        classify_error returns 'transient' — gateway enqueues for retry."""
        with self._patch_status_raises(ObsidianServerError(503)):
            with pytest.raises(ObsidianServerError) as excinfo:
                write_file_raw("x.md", "content")
            assert excinfo.value.status == 503


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
            side_effect=ObsidianEditorConflict("x.md"),
        ):
            with pytest.raises(ObsidianEditorConflict):
                vault_writer.vault_write(
                    "x.md", Path("/tmp/x.md"), "new content"
                )
