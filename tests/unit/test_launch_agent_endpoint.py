"""Unit tests for POST /api/launch-agent dashboard endpoint."""

from unittest.mock import patch

import pytest


@pytest.fixture
def client():
    """Flask test client for the dashboard app."""
    from work_buddy.dashboard.service import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _patch_launch(**begin_kw):
    """Context manager that patches consent + begin_session for launch tests.

    grant_consent and begin_session are local imports inside the endpoint,
    so we patch at the source module level.
    """
    from contextlib import ExitStack
    stack = ExitStack()
    mock_begin = stack.enter_context(
        patch("work_buddy.session_launcher.begin_session",
              return_value=begin_kw or {"status": "ok", "pid": 1, "message": "ok"})
    )
    mock_grant = stack.enter_context(
        patch("work_buddy.consent.grant_consent")
    )
    return stack, mock_begin, mock_grant


@pytest.mark.unit
class TestLaunchAgentValidation:
    """Input validation for /api/launch-agent."""

    def test_empty_body_returns_400(self, client):
        with patch("work_buddy.dashboard.service._is_read_only", return_value=False):
            resp = client.post("/api/launch-agent", json={})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "No prompt" in data["error"]

    def test_missing_prompt_returns_400(self, client):
        with patch("work_buddy.dashboard.service._is_read_only", return_value=False):
            resp = client.post("/api/launch-agent", json={"mode": "desktop"})
        assert resp.status_code == 400

    def test_invalid_mode_returns_400(self, client):
        with patch("work_buddy.dashboard.service._is_read_only", return_value=False):
            resp = client.post("/api/launch-agent", json={
                "prompt": "hello",
                "mode": "invalid",
            })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "Unknown mode" in data["error"]


@pytest.mark.unit
class TestLaunchAgentReadOnly:
    """Read-only mode blocks agent launches."""

    def test_read_only_returns_403(self, client):
        with patch("work_buddy.dashboard.service._is_read_only", return_value=True):
            resp = client.post("/api/launch-agent", json={
                "prompt": "/wb-setup diagnose hindsight",
                "mode": "desktop",
            })
        assert resp.status_code == 403


@pytest.mark.unit
class TestLaunchAgentModes:
    """Desktop vs mobile mode behavior."""

    @patch("work_buddy.dashboard.service._is_read_only", return_value=False)
    def test_desktop_mode_passes_remote_control_false(self, mock_ro, client):
        stack, mock_begin, _ = _patch_launch(
            status="ok", pid=999, message="ok",
        )
        with stack:
            resp = client.post("/api/launch-agent", json={
                "prompt": "hello", "mode": "desktop",
            })
            data = resp.get_json()
            assert data["success"] is True
            assert data["mode"] == "desktop"
            _, kwargs = mock_begin.call_args
            assert kwargs["remote_control"] is False

    @patch("work_buddy.dashboard.service._is_read_only", return_value=False)
    def test_mobile_mode_passes_remote_control_true(self, mock_ro, client):
        stack, mock_begin, _ = _patch_launch(
            status="ok", pid=999, message="ok",
        )
        with stack:
            resp = client.post("/api/launch-agent", json={
                "prompt": "hello", "mode": "mobile",
            })
            data = resp.get_json()
            assert data["success"] is True
            assert data["mode"] == "mobile"
            _, kwargs = mock_begin.call_args
            assert kwargs["remote_control"] is True

    @patch("work_buddy.dashboard.service._is_read_only", return_value=False)
    def test_default_mode_is_desktop(self, mock_ro, client):
        stack, mock_begin, _ = _patch_launch(
            status="ok", pid=999, message="ok",
        )
        with stack:
            resp = client.post("/api/launch-agent", json={"prompt": "hello"})
            data = resp.get_json()
            assert data["mode"] == "desktop"
            _, kwargs = mock_begin.call_args
            assert kwargs["remote_control"] is False


@pytest.mark.unit
class TestLaunchAgentConsent:
    """Consent is auto-granted before launching."""

    @patch("work_buddy.dashboard.service._is_read_only", return_value=False)
    def test_grants_consent_before_begin_session(self, mock_ro, client):
        call_order = []

        def track_grant(*a, **kw):
            call_order.append("grant")

        def track_begin(*a, **kw):
            call_order.append("begin")
            return {"status": "ok", "pid": 1, "message": "ok"}

        with patch("work_buddy.session_launcher.begin_session", side_effect=track_begin), \
             patch("work_buddy.consent.grant_consent", side_effect=track_grant):
            client.post("/api/launch-agent", json={"prompt": "hello"})

        assert call_order == ["grant", "begin"]


@pytest.mark.unit
class TestLaunchAgentErrorHandling:
    """Error handling for launch failures."""

    @patch("work_buddy.dashboard.service._is_read_only", return_value=False)
    def test_begin_session_error_returns_500(self, mock_ro, client):
        stack, mock_begin, _ = _patch_launch(
            status="error", error="claude not found",
        )
        with stack:
            resp = client.post("/api/launch-agent", json={"prompt": "hello"})
            assert resp.status_code == 500
            data = resp.get_json()
            assert data["success"] is False
            assert "claude not found" in data["error"]

    @patch("work_buddy.dashboard.service._is_read_only", return_value=False)
    def test_exception_returns_500(self, mock_ro, client):
        with patch("work_buddy.session_launcher.begin_session") as mock_begin, \
             patch("work_buddy.consent.grant_consent"):
            mock_begin.side_effect = RuntimeError("unexpected")
            resp = client.post("/api/launch-agent", json={"prompt": "hello"})
            assert resp.status_code == 500
            data = resp.get_json()
            assert data["success"] is False

    @patch("work_buddy.dashboard.service._is_read_only", return_value=False)
    def test_success_returns_pid(self, mock_ro, client):
        stack, mock_begin, _ = _patch_launch(
            status="ok", pid=42, message="launched",
        )
        with stack:
            resp = client.post("/api/launch-agent", json={"prompt": "hello"})
            data = resp.get_json()
            assert data["success"] is True
            assert data["pid"] == 42
