"""Unit tests for session_launcher — command construction and remote_control flag."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.mark.unit
class TestDoStartCommandConstruction:
    """Verify _do_start builds the correct CLI command for each mode."""

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_remote_control_true_includes_flag(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        result = _do_start(cwd="/tmp", prompt="hello", remote_control=True)

        cmd = mock_launch.call_args[0][0]
        assert "--remote-control" in cmd
        assert result["status"] == "ok"
        assert result["remote_control"] is True

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_remote_control_false_excludes_flag(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        result = _do_start(cwd="/tmp", prompt="hello", remote_control=False)

        cmd = mock_launch.call_args[0][0]
        assert "--remote-control" not in cmd
        assert result["status"] == "ok"
        assert result["remote_control"] is False

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_prompt_is_first_positional_arg(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        _do_start(cwd="/tmp", prompt="do the thing", remote_control=True)

        cmd = mock_launch.call_args[0][0]
        assert cmd[0] == "claude"
        assert cmd[1] == "do the thing"

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_bypass_permissions_adds_flag(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        _do_start(cwd="/tmp", prompt="hello", bypass_permissions=True, remote_control=False)

        cmd = mock_launch.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_bypass_permissions_false_excludes_flag(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        _do_start(cwd="/tmp", prompt="hello", bypass_permissions=False, remote_control=False)

        cmd = mock_launch.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_default_prompt_when_none(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        _do_start(cwd="/tmp", prompt=None, remote_control=False)

        cmd = mock_launch.call_args[0][0]
        # Should have a default prompt, not None
        assert cmd[1] is not None
        assert isinstance(cmd[1], str)
        assert len(cmd[1]) > 0

    @patch("work_buddy.session_launcher._launch_and_verify")
    def test_launch_error_returns_error_dict(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        mock_launch.return_value = {"status": "error", "error": "claude not found"}

        result = _do_start(cwd="/tmp", prompt="hello", remote_control=False)

        assert result["status"] == "error"

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_remote_message_includes_connect_url(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        result = _do_start(cwd="/tmp", prompt="hello", remote_control=True)
        assert "claude.ai/code" in result["message"]

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_local_message_excludes_connect_url(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        result = _do_start(cwd="/tmp", prompt="hello", remote_control=False)
        assert "claude.ai/code" not in result["message"]


@pytest.mark.unit
class TestDoResumeCommandConstruction:
    """Verify _do_resume builds the correct CLI command for each mode."""

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    @patch("work_buddy.session_launcher._find_session_id", return_value="sess-abc123")
    def test_remote_control_true_includes_flag(self, mock_find, mock_launch):
        from work_buddy.session_launcher import _do_resume

        result = _do_resume(
            session_id="sess-abc123", session_name=None, cwd="/tmp",
            remote_control=True,
        )

        cmd = mock_launch.call_args[0][0]
        assert "--remote-control" in cmd
        assert "--resume" in cmd
        assert result["remote_control"] is True

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    @patch("work_buddy.session_launcher._find_session_id", return_value="sess-abc123")
    def test_remote_control_false_excludes_flag(self, mock_find, mock_launch):
        from work_buddy.session_launcher import _do_resume

        result = _do_resume(
            session_id="sess-abc123", session_name=None, cwd="/tmp",
            remote_control=False,
        )

        cmd = mock_launch.call_args[0][0]
        assert "--remote-control" not in cmd
        assert result["remote_control"] is False


@pytest.mark.unit
class TestBeginSessionDispatch:
    """Verify begin_session routes to the right internal function."""

    @patch("work_buddy.session_launcher._check_remote_session_consent", return_value=True)
    @patch("work_buddy.session_launcher._do_start")
    def test_new_session_passes_remote_control(self, mock_start, mock_consent):
        from work_buddy.session_launcher import begin_session

        mock_start.return_value = {"status": "ok", "pid": 1}

        begin_session(prompt="hello", remote_control=False)

        _, kwargs = mock_start.call_args
        assert kwargs["remote_control"] is False

    @patch("work_buddy.session_launcher._check_remote_session_consent", return_value=True)
    @patch("work_buddy.session_launcher._do_start")
    def test_remote_control_defaults_to_true(self, mock_start, mock_consent):
        from work_buddy.session_launcher import begin_session

        mock_start.return_value = {"status": "ok", "pid": 1}

        begin_session(prompt="hello")

        _, kwargs = mock_start.call_args
        assert kwargs["remote_control"] is True

    @patch("work_buddy.session_launcher._check_remote_session_consent", return_value=False)
    def test_consent_required_returns_early(self, mock_consent):
        from work_buddy.session_launcher import begin_session

        result = begin_session(prompt="hello")

        assert result["status"] == "consent_required"

    @patch("work_buddy.session_launcher._check_remote_session_consent", return_value=True)
    @patch("work_buddy.session_launcher._do_resume")
    def test_resume_passes_remote_control(self, mock_resume, mock_consent):
        from work_buddy.session_launcher import begin_session

        mock_resume.return_value = {"status": "ok", "pid": 1}

        begin_session(session_id="sess-123", remote_control=False)

        _, kwargs = mock_resume.call_args
        assert kwargs["remote_control"] is False
