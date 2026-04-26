"""Unit tests for the typed Obsidian error hierarchy.

Covers:
- isinstance relations across the hierarchy (each subclass IS-A every parent)
- error_kind values match the documented canonical strings
- Carrier fields populated correctly (path, content_hint, write_mode, status, body)
- ObsidianEditorConflict constructor preserves the legacy
  EditorConflict(path, reason="editor_dirty") signature for backward compat
- Module exposes EditorConflict as an alias of ObsidianEditorConflict
"""
from __future__ import annotations

import pytest

from work_buddy.obsidian.errors import (
    EditorConflict,  # alias under test
    ObsidianEditorConflict,
    ObsidianError,
    ObsidianHTTPError,
    ObsidianNotRunning,
    ObsidianPluginDisabled,
    ObsidianPluginMissing,
    ObsidianPostWriteUncertain,
    ObsidianRefused,
    ObsidianServerError,
    ObsidianStartupRace,
    ObsidianTimeout,
    ObsidianUnreachable,
)


class TestHierarchy:
    """Each subclass IS-A every documented ancestor."""

    @pytest.mark.parametrize("cls", [
        ObsidianUnreachable,
        ObsidianNotRunning,
        ObsidianPluginMissing,
        ObsidianPluginDisabled,
        ObsidianStartupRace,
        ObsidianTimeout,
        ObsidianPostWriteUncertain,
        ObsidianHTTPError,
        ObsidianEditorConflict,
        ObsidianRefused,
        ObsidianServerError,
    ])
    def test_subclass_of_obsidian_error(self, cls):
        assert issubclass(cls, ObsidianError)

    @pytest.mark.parametrize("cls", [
        ObsidianNotRunning,
        ObsidianPluginMissing,
        ObsidianPluginDisabled,
        ObsidianStartupRace,
    ])
    def test_unreachable_subclasses(self, cls):
        assert issubclass(cls, ObsidianUnreachable)

    def test_post_write_uncertain_is_timeout(self):
        assert issubclass(ObsidianPostWriteUncertain, ObsidianTimeout)

    @pytest.mark.parametrize("cls", [
        ObsidianEditorConflict,
        ObsidianRefused,
        ObsidianServerError,
    ])
    def test_http_error_subclasses(self, cls):
        assert issubclass(cls, ObsidianHTTPError)

    def test_unreachable_not_a_timeout(self):
        """Sibling categories must not collapse — semantically distinct."""
        assert not issubclass(ObsidianUnreachable, ObsidianTimeout)
        assert not issubclass(ObsidianTimeout, ObsidianUnreachable)

    def test_unreachable_not_an_http_error(self):
        assert not issubclass(ObsidianUnreachable, ObsidianHTTPError)

    def test_timeout_not_an_http_error(self):
        assert not issubclass(ObsidianTimeout, ObsidianHTTPError)


class TestErrorKinds:
    """Canonical error_kind strings — these survive serialization."""

    @pytest.mark.parametrize("cls,expected", [
        (ObsidianError, "obsidian_unknown"),
        (ObsidianUnreachable, "obsidian_unreachable"),
        (ObsidianNotRunning, "obsidian_not_running"),
        (ObsidianPluginMissing, "obsidian_plugin_missing"),
        (ObsidianPluginDisabled, "obsidian_plugin_disabled"),
        (ObsidianStartupRace, "obsidian_startup_race"),
        (ObsidianTimeout, "obsidian_timeout"),
        (ObsidianPostWriteUncertain, "obsidian_post_write_uncertain"),
        (ObsidianHTTPError, "obsidian_http_error"),
        (ObsidianEditorConflict, "obsidian_editor_conflict"),
        (ObsidianRefused, "obsidian_refused"),
        (ObsidianServerError, "obsidian_server_error"),
    ])
    def test_class_level_error_kind(self, cls, expected):
        assert cls.error_kind == expected

    @pytest.mark.parametrize("instance,expected", [
        (ObsidianNotRunning(), "obsidian_not_running"),
        (ObsidianTimeout(), "obsidian_timeout"),
        (ObsidianPostWriteUncertain("x.md"), "obsidian_post_write_uncertain"),
        (ObsidianRefused(403), "obsidian_refused"),
        (ObsidianServerError(500), "obsidian_server_error"),
        (ObsidianEditorConflict("x.md"), "obsidian_editor_conflict"),
    ])
    def test_instance_inherits_error_kind(self, instance, expected):
        """Instances see the class attribute via normal attribute lookup."""
        assert instance.error_kind == expected


class TestPostWriteUncertainCarriers:
    """ObsidianPostWriteUncertain carries (path, content_hint, write_mode)."""

    def test_default_write_mode_is_replace(self):
        exc = ObsidianPostWriteUncertain("notes/x.md")
        assert exc.path == "notes/x.md"
        assert exc.content_hint is None
        assert exc.write_mode == "replace"

    def test_carries_explicit_fields(self):
        exc = ObsidianPostWriteUncertain(
            "notes/x.md",
            content_hint="hello world",
            write_mode="insert",
        )
        assert exc.path == "notes/x.md"
        assert exc.content_hint == "hello world"
        assert exc.write_mode == "insert"

    def test_message_includes_diagnostic_info(self):
        exc = ObsidianPostWriteUncertain(
            "notes/x.md", content_hint="hi", write_mode="append",
        )
        assert "obsidian_post_write_uncertain" in str(exc)
        assert "notes/x.md" in str(exc)
        assert "append" in str(exc)

    def test_kwargs_only_for_optional_fields(self):
        """content_hint and write_mode are keyword-only — protects against
        positional confusion if more fields are added later."""
        with pytest.raises(TypeError):
            ObsidianPostWriteUncertain("x.md", "hello", "insert")  # type: ignore[misc]


class TestHTTPErrorCarriers:
    """ObsidianHTTPError and subclasses carry (status, body)."""

    def test_status_only(self):
        exc = ObsidianRefused(403)
        assert exc.status == 403
        assert exc.body is None

    def test_status_and_body(self):
        body = {"error": "permission_denied"}
        exc = ObsidianServerError(503, body=body)
        assert exc.status == 503
        assert exc.body == body

    def test_message_includes_status_and_kind(self):
        exc = ObsidianRefused(404)
        msg = str(exc)
        assert "404" in msg
        assert "obsidian_refused" in msg


class TestEditorConflictBackwardCompat:
    """ObsidianEditorConflict preserves the legacy EditorConflict signature."""

    def test_legacy_constructor_path_only(self):
        exc = ObsidianEditorConflict("tasks/master-task-list.md")
        assert exc.path == "tasks/master-task-list.md"
        assert exc.reason == "editor_dirty"
        assert exc.status == 409  # implicit, since it's a 409

    def test_legacy_constructor_with_reason(self):
        exc = ObsidianEditorConflict("tasks/x.md", "custom_reason")
        assert exc.path == "tasks/x.md"
        assert exc.reason == "custom_reason"

    def test_legacy_message_format(self):
        """The original EditorConflict produced 'editor_dirty: <path>'.
        Preserve byte-for-byte so legacy log scrapers and string-pattern
        matchers (the very thing this refactor is replacing!) continue
        to work during the transition window (CP1-CP8)."""
        exc = ObsidianEditorConflict("foo/bar.md")
        assert str(exc) == "editor_dirty: foo/bar.md"

    def test_message_format_with_custom_reason(self):
        exc = ObsidianEditorConflict("foo.md", "user_typing")
        assert str(exc) == "user_typing: foo.md"

    def test_alias_is_same_class(self):
        """`from work_buddy.obsidian.errors import EditorConflict` resolves
        to ObsidianEditorConflict — the alias preserves call sites that
        haven't migrated yet."""
        assert EditorConflict is ObsidianEditorConflict

    def test_alias_can_construct(self):
        exc = EditorConflict("x.md")
        assert isinstance(exc, ObsidianEditorConflict)
        assert exc.path == "x.md"

    def test_alias_can_catch(self):
        """except EditorConflict catches ObsidianEditorConflict instances."""
        try:
            raise ObsidianEditorConflict("x.md")
        except EditorConflict as exc:
            assert exc.path == "x.md"
        else:
            pytest.fail("should have caught via alias")


class TestRaiseAndCatchPolymorphism:
    """The whole point of typed exceptions: catch by category, not by name."""

    def test_catch_any_obsidian_error(self):
        for exc in [
            ObsidianNotRunning(),
            ObsidianTimeout(),
            ObsidianPostWriteUncertain("x.md"),
            ObsidianRefused(403),
            ObsidianServerError(500),
            ObsidianEditorConflict("x.md"),
            ObsidianStartupRace(),
        ]:
            try:
                raise exc
            except ObsidianError as caught:
                assert caught is exc
            else:
                pytest.fail(f"{type(exc).__name__} not caught by ObsidianError")

    def test_catch_any_unreachable(self):
        for exc in [
            ObsidianNotRunning(),
            ObsidianPluginMissing(),
            ObsidianPluginDisabled(),
            ObsidianStartupRace(),
        ]:
            try:
                raise exc
            except ObsidianUnreachable as caught:
                assert caught is exc
            else:
                pytest.fail(f"{type(exc).__name__} not caught by ObsidianUnreachable")

    def test_catch_post_write_uncertain_via_timeout(self):
        """ObsidianPostWriteUncertain IS-A ObsidianTimeout — generic
        timeout handlers catch it, but specialized handlers can pick it
        out specifically before falling through."""
        try:
            raise ObsidianPostWriteUncertain("x.md")
        except ObsidianTimeout as caught:
            assert isinstance(caught, ObsidianPostWriteUncertain)

    def test_catch_any_http_error(self):
        for exc in [
            ObsidianEditorConflict("x.md"),
            ObsidianRefused(403),
            ObsidianServerError(500),
        ]:
            try:
                raise exc
            except ObsidianHTTPError as caught:
                assert caught.status > 0
            else:
                pytest.fail(f"{type(exc).__name__} not caught by ObsidianHTTPError")
