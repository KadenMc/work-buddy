"""Unit tests for typed-exception translation in bridge._request_with_status.

Covers:
- urllib failures → typed ObsidianError subclasses
- HTTP statuses → typed ObsidianHTTPError subclasses
- _last_failure_kind / _last_failure_status preserved as legacy strings
  for the dashboard sparkline (work_buddy/dashboard/api.py:get_bridge_status)
- _make_content_hint produces the right shape per write_mode
- _refine_unreachable_kind picks the right ObsidianUnreachable subclass

These are CP2 tests. The post-write-uncertain detection lives in
write_file_raw (covered by test_editor_conflict.py post-CP2 update).
"""
from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from work_buddy.obsidian import bridge
from work_buddy.obsidian.errors import (
    ObsidianEditorConflict,
    ObsidianError,
    ObsidianHTTPError,
    ObsidianNotRunning,
    ObsidianPluginDisabled,
    ObsidianPluginMissing,
    ObsidianRefused,
    ObsidianServerError,
    ObsidianStartupRace,
    ObsidianTimeout,
    ObsidianUnreachable,
)


@pytest.fixture(autouse=True)
def reset_bridge_module_state():
    """Reset _last_failure_* before each test so assertions aren't polluted
    by prior test runs."""
    bridge._last_failure_kind = ""
    bridge._last_failure_status = None
    bridge._last_failure_reason = ""
    bridge._consecutive_failures = 0
    yield
    bridge._last_failure_kind = ""
    bridge._last_failure_status = None
    bridge._last_failure_reason = ""
    bridge._consecutive_failures = 0


def _make_http_error(status: int, body_json: dict | None = None) -> HTTPError:
    """Build an HTTPError that read() returns the given body for."""
    import io

    body_bytes = (
        b"" if body_json is None
        else __import__("json").dumps(body_json).encode("utf-8")
    )
    return HTTPError(
        url="http://test/files/x.md",
        code=status,
        msg=f"HTTP {status}",
        hdrs=None,
        fp=io.BytesIO(body_bytes),
    )


# ---------------------------------------------------------------------------
# _make_content_hint
# ---------------------------------------------------------------------------


class TestMakeContentHint:
    """Verification hint shape per write_mode."""

    def test_replace_mode_uses_sha256(self):
        h = bridge._make_content_hint("hello world", "replace")
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64  # 32 bytes hex-encoded

    def test_replace_is_deterministic(self):
        h1 = bridge._make_content_hint("foo bar", "replace")
        h2 = bridge._make_content_hint("foo bar", "replace")
        assert h1 == h2

    def test_replace_distinguishes_content(self):
        h1 = bridge._make_content_hint("foo", "replace")
        h2 = bridge._make_content_hint("bar", "replace")
        assert h1 != h2

    def test_insert_mode_uses_substring_witness(self):
        content = "x" * 500
        h = bridge._make_content_hint(content, "insert")
        assert h == "x" * 256
        assert not h.startswith("sha256:")

    def test_append_mode_uses_substring_witness(self):
        content = "abc" * 100  # 300 chars
        h = bridge._make_content_hint(content, "append")
        assert len(h) == 256
        assert h == content[:256]

    def test_short_insert_returns_full_content(self):
        h = bridge._make_content_hint("short text", "insert")
        assert h == "short text"


# ---------------------------------------------------------------------------
# _http_status_to_exception_type
# ---------------------------------------------------------------------------


class TestHTTPStatusMapping:
    @pytest.mark.parametrize("status,expected", [
        (409, ObsidianEditorConflict),
        (400, ObsidianRefused),
        (401, ObsidianRefused),
        (403, ObsidianRefused),
        (404, ObsidianRefused),
        (422, ObsidianRefused),
        (500, ObsidianServerError),
        (502, ObsidianServerError),
        (503, ObsidianServerError),
        (504, ObsidianServerError),
    ])
    def test_status_maps_to_class(self, status, expected):
        assert bridge._http_status_to_exception_type(status) is expected


# ---------------------------------------------------------------------------
# _refine_unreachable_kind
# ---------------------------------------------------------------------------


class TestRefineUnreachable:
    """The slow-path disambiguation that picks the right ObsidianUnreachable
    subclass after a connection failure."""

    def test_obsidian_not_running(self):
        with patch("work_buddy.obsidian.bridge.is_obsidian_running", return_value=False):
            assert bridge._refine_unreachable_kind() is ObsidianNotRunning

    def test_plugin_missing(self):
        with patch("work_buddy.obsidian.bridge.is_obsidian_running", return_value=True), \
             patch(
                 "work_buddy.health.requirement_checks.get_work_buddy_plugin_state",
                 return_value=("not_installed", "no manifest"),
             ):
            assert bridge._refine_unreachable_kind() is ObsidianPluginMissing

    def test_plugin_disabled(self):
        with patch("work_buddy.obsidian.bridge.is_obsidian_running", return_value=True), \
             patch(
                 "work_buddy.health.requirement_checks.get_work_buddy_plugin_state",
                 return_value=("disabled", "not in community-plugins.json"),
             ):
            assert bridge._refine_unreachable_kind() is ObsidianPluginDisabled

    def test_startup_race(self):
        """Plugin enabled but port still refused — Obsidian just started,
        plugin not loaded yet, or a port-binding race."""
        with patch("work_buddy.obsidian.bridge.is_obsidian_running", return_value=True), \
             patch(
                 "work_buddy.health.requirement_checks.get_work_buddy_plugin_state",
                 return_value=("ok", "manifest present, enabled"),
             ):
            assert bridge._refine_unreachable_kind() is ObsidianStartupRace

    def test_helper_failure_falls_back_to_base(self):
        """If is_obsidian_running raises, return generic ObsidianUnreachable
        rather than masking the original failure with a secondary error."""
        with patch(
            "work_buddy.obsidian.bridge.is_obsidian_running",
            side_effect=RuntimeError("process check failed"),
        ):
            assert bridge._refine_unreachable_kind() is ObsidianUnreachable

    def test_plugin_state_failure_falls_back_to_base(self):
        with patch("work_buddy.obsidian.bridge.is_obsidian_running", return_value=True), \
             patch(
                 "work_buddy.health.requirement_checks.get_work_buddy_plugin_state",
                 side_effect=RuntimeError("filesystem check failed"),
             ):
            assert bridge._refine_unreachable_kind() is ObsidianUnreachable


# ---------------------------------------------------------------------------
# _request_with_status — happy path
# ---------------------------------------------------------------------------


class TestRequestWithStatusSuccess:
    """Successful bridge calls return (status, body) and clear failure state."""

    def _make_resp(self, status, body_json):
        """Build a fake urlopen context manager with the given response."""
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = (
            __import__("json").dumps(body_json).encode("utf-8")
            if body_json is not None else b""
        )
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    def test_200_returns_status_and_body(self):
        with patch(
            "work_buddy.obsidian.bridge.urlopen",
            return_value=self._make_resp(200, {"path": "x.md"}),
        ):
            status, body = bridge._request_with_status("GET", "/files/x.md")
            assert status == 200
            assert body == {"path": "x.md"}

    def test_204_returns_none_body(self):
        with patch(
            "work_buddy.obsidian.bridge.urlopen",
            return_value=self._make_resp(204, None),
        ):
            status, body = bridge._request_with_status("DELETE", "/files/x.md")
            assert status == 204
            assert body is None

    def test_success_clears_failure_kind(self):
        # Pre-pollute failure state.
        bridge._last_failure_kind = "timeout"
        bridge._last_failure_status = 503
        with patch(
            "work_buddy.obsidian.bridge.urlopen",
            return_value=self._make_resp(200, {}),
        ):
            bridge._request_with_status("GET", "/files/x.md")
        assert bridge._last_failure_kind == ""
        assert bridge._last_failure_status is None


# ---------------------------------------------------------------------------
# _request_with_status — HTTP errors
# ---------------------------------------------------------------------------


class TestRequestWithStatusHTTPErrors:
    """Non-2xx HTTP responses raise typed ObsidianHTTPError subclasses."""

    @pytest.mark.parametrize("status,expected_cls", [
        (400, ObsidianRefused),
        (401, ObsidianRefused),
        (404, ObsidianRefused),
        (422, ObsidianRefused),
        (500, ObsidianServerError),
        (503, ObsidianServerError),
    ])
    def test_http_error_raises_typed(self, status, expected_cls):
        err = _make_http_error(status, {"error": "test"})
        with patch("work_buddy.obsidian.bridge.urlopen", side_effect=err):
            with pytest.raises(expected_cls) as excinfo:
                bridge._request_with_status("GET", "/files/x.md")
            assert excinfo.value.status == status
            assert excinfo.value.body == {"error": "test"}

    def test_409_raises_editor_conflict_with_path(self):
        err = _make_http_error(409, {"error": "editor_dirty"})
        with patch("work_buddy.obsidian.bridge.urlopen", side_effect=err):
            with pytest.raises(ObsidianEditorConflict) as excinfo:
                bridge._request_with_status("PUT", "/files/notes%2Fx.md")
            # The handler decodes the URL path and strips /files/.
            assert excinfo.value.path == "notes/x.md"
            assert excinfo.value.status == 409
            assert excinfo.value.body == {"error": "editor_dirty"}

    def test_409_message_format_preserved(self):
        """Backward-compat: 'editor_dirty: <path>' for log scrapers."""
        err = _make_http_error(409, {"error": "editor_dirty"})
        with patch("work_buddy.obsidian.bridge.urlopen", side_effect=err):
            with pytest.raises(ObsidianEditorConflict) as excinfo:
                bridge._request_with_status("PUT", "/files/x.md")
            assert str(excinfo.value) == "editor_dirty: x.md"

    def test_http_error_sets_failure_kind(self):
        err = _make_http_error(500, {"error": "internal"})
        with patch("work_buddy.obsidian.bridge.urlopen", side_effect=err):
            with pytest.raises(ObsidianServerError):
                bridge._request_with_status("GET", "/files/x.md")
        assert bridge._last_failure_kind == "http_error"
        assert bridge._last_failure_status == 500

    def test_http_error_does_not_bump_consecutive_failures(self):
        """4xx/5xx is application-level, not connectivity. Latency
        sparkline should not count it as a connectivity failure."""
        bridge._consecutive_failures = 0
        err = _make_http_error(400, {})
        with patch("work_buddy.obsidian.bridge.urlopen", side_effect=err):
            with pytest.raises(ObsidianRefused):
                bridge._request_with_status("GET", "/files/x.md")
        assert bridge._consecutive_failures == 0


# ---------------------------------------------------------------------------
# _request_with_status — connectivity failures
# ---------------------------------------------------------------------------


class TestRequestWithStatusConnectivityFailures:
    """urllib timeouts / connection errors raise typed exceptions."""

    def test_timeout_with_port_open_raises_timeout(self):
        with patch(
            "work_buddy.obsidian.bridge.urlopen",
            side_effect=TimeoutError("read timed out"),
        ), patch(
            "work_buddy.obsidian.bridge._probe_port_open", return_value=True,
        ):
            with pytest.raises(ObsidianTimeout) as excinfo:
                bridge._request_with_status("GET", "/files/x.md")
            # Should be plain ObsidianTimeout, NOT ObsidianUnreachable.
            assert type(excinfo.value) is ObsidianTimeout
        assert bridge._last_failure_kind == "timeout"

    def test_timeout_with_port_closed_raises_unreachable(self):
        """Windows often surfaces TCP-connect-timeout as socket.timeout
        rather than ECONNREFUSED. The TCP probe disambiguates."""
        with patch(
            "work_buddy.obsidian.bridge.urlopen",
            side_effect=TimeoutError("connect timed out"),
        ), patch(
            "work_buddy.obsidian.bridge._probe_port_open", return_value=False,
        ), patch(
            "work_buddy.obsidian.bridge.is_obsidian_running", return_value=False,
        ):
            with pytest.raises(ObsidianUnreachable) as excinfo:
                bridge._request_with_status("GET", "/files/x.md")
            # The disambiguator picks ObsidianNotRunning subclass.
            assert isinstance(excinfo.value, ObsidianNotRunning)
        assert bridge._last_failure_kind == "unreachable"

    def test_connection_refused_raises_unreachable(self):
        """ConnectionRefusedError is unambiguous — TCP refused, body not sent."""
        wrapped = URLError(ConnectionRefusedError(111, "Connection refused"))
        with patch(
            "work_buddy.obsidian.bridge.urlopen", side_effect=wrapped,
        ), patch(
            "work_buddy.obsidian.bridge.is_obsidian_running", return_value=False,
        ):
            with pytest.raises(ObsidianUnreachable):
                bridge._request_with_status("GET", "/files/x.md")
        assert bridge._last_failure_kind == "unreachable"

    def test_connectivity_failure_bumps_consecutive_failures(self):
        bridge._consecutive_failures = 0
        with patch(
            "work_buddy.obsidian.bridge.urlopen",
            side_effect=TimeoutError("timed out"),
        ), patch(
            "work_buddy.obsidian.bridge._probe_port_open", return_value=True,
        ):
            with pytest.raises(ObsidianTimeout):
                bridge._request_with_status("GET", "/files/x.md")
        assert bridge._consecutive_failures == 1


# ---------------------------------------------------------------------------
# Dashboard contract — _last_failure_kind preserved
# ---------------------------------------------------------------------------


class TestDashboardContractPreserved:
    """The dashboard sparkline (work_buddy/dashboard/api.py::get_bridge_status)
    consumes _last_failure_kind strings to pick bar classes. CP2 must
    keep populating these strings even though we now raise typed
    exceptions internally."""

    @pytest.mark.parametrize("exc_cls,expected_kind", [
        (ObsidianTimeout, "timeout"),
        (ObsidianUnreachable, "unreachable"),
        (ObsidianHTTPError, "http_error"),
    ])
    def test_kind_mapping(self, exc_cls, expected_kind):
        kind, _status = bridge._exception_to_failure_kind(exc_cls)
        assert kind == expected_kind

    def test_unreachable_subclasses_all_map_to_unreachable(self):
        for cls in [ObsidianNotRunning, ObsidianPluginMissing,
                    ObsidianPluginDisabled, ObsidianStartupRace]:
            kind, _ = bridge._exception_to_failure_kind(cls)
            assert kind == "unreachable", f"{cls.__name__} mapped to {kind!r}"

    def test_http_subclasses_all_map_to_http_error(self):
        for cls in [ObsidianEditorConflict, ObsidianRefused, ObsidianServerError]:
            kind, _ = bridge._exception_to_failure_kind(cls)
            assert kind == "http_error", f"{cls.__name__} mapped to {kind!r}"

    def test_timeout_subclass_maps_to_timeout(self):
        from work_buddy.obsidian.errors import ObsidianPostWriteUncertain
        kind, _ = bridge._exception_to_failure_kind(ObsidianPostWriteUncertain)
        assert kind == "timeout"


# ---------------------------------------------------------------------------
# Re-exports of the typed Obsidian exceptions at the bridge module
# ---------------------------------------------------------------------------


class TestBridgeReExports:
    def test_typed_exceptions_re_exported(self):
        for name in [
            "ObsidianError",
            "ObsidianUnreachable",
            "ObsidianNotRunning",
            "ObsidianPluginMissing",
            "ObsidianPluginDisabled",
            "ObsidianStartupRace",
            "ObsidianTimeout",
            "ObsidianPostWriteUncertain",
            "ObsidianHTTPError",
            "ObsidianEditorConflict",
            "ObsidianRefused",
            "ObsidianServerError",
        ]:
            assert hasattr(bridge, name), f"bridge missing re-export of {name}"

    def test_legacy_editor_conflict_alias_removed_in_cp9(self):
        """The legacy ``bridge.EditorConflict`` alias was removed in CP9.
        Existing callers must import ``ObsidianEditorConflict`` directly.
        Re-introducing the alias would muddy the type system."""
        assert not hasattr(bridge, "EditorConflict"), (
            "bridge.EditorConflict alias was removed in CP9 — see DECISIONS.md"
        )
