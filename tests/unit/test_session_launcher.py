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

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    def test_codex_start_uses_native_command_without_claude_flags(self, mock_launch):
        from work_buddy.session_launcher import _do_start

        _do_start(
            cwd="/tmp",
            prompt="hello",
            remote_control=False,
            harness_id="codexcli",
        )

        cmd = mock_launch.call_args[0][0]
        assert cmd == ["codex", "hello"]


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

    @patch("work_buddy.session_launcher._launch_and_verify", return_value=12345)
    @patch("work_buddy.session_launcher._find_session_id", return_value="codex-thread")
    def test_codex_resume_uses_native_subcommand(self, mock_find, mock_launch):
        from work_buddy.session_launcher import _do_resume

        result = _do_resume(
            session_id="codex-thread",
            session_name=None,
            cwd="/tmp",
            remote_control=False,
            harness_id="codexcli",
        )

        assert mock_launch.call_args[0][0] == ["codex", "resume", "codex-thread"]
        assert result["status"] == "ok"


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

    @patch("work_buddy.session_launcher._check_remote_session_consent", return_value=True)
    def test_codex_rejects_remote_control(self, mock_consent):
        from work_buddy.session_launcher import begin_session

        result = begin_session(prompt="hello", harness_id="codexcli")

        assert result["status"] == "error"
        assert "Remote control" in result["error"]


@pytest.mark.unit
class TestVisibleTerminalLaunch:
    """Verify the platform launchers hand the right argv to the process package.

    Each one opens its terminal through
    ``work_buddy.process.open_visible_terminal`` rather than ``subprocess``
    directly, so the recording runner captures the resolved launch instead of
    a real terminal window opening.
    """

    def test_windows_wraps_argv_in_powershell_and_strips_claude_env(
        self, monkeypatch, recording_runner,
    ):
        from work_buddy.process import policy
        from work_buddy.process.policy import CREATE_NEW_CONSOLE, Intent
        from work_buddy.session_launcher import _launch_windows

        monkeypatch.setattr(policy, "is_windows", lambda platform=None: True)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")

        pid = _launch_windows(["claude", "hello there"], "C:/repo")

        [launch] = recording_runner.launches
        assert launch.intent is Intent.VISIBLE_TERMINAL
        assert launch.argv[0] == "powershell.exe"
        assert launch.argv[1] == "-NoExit"
        assert launch.argv[2] == "-Command"
        assert launch.argv[3] == "cd 'C:/repo'; & 'claude' 'hello there'"
        assert launch.creationflags == CREATE_NEW_CONSOLE
        assert "ANTHROPIC_API_KEY" not in launch.env
        assert "CLAUDE_CODE_ENTRYPOINT" not in launch.env
        assert pid == recording_runner.processes[0].pid

    def test_windows_prefers_pwsh_when_installed(self, monkeypatch, recording_runner):
        from work_buddy.process import policy
        from work_buddy.session_launcher import _launch_windows

        monkeypatch.setattr(policy, "is_windows", lambda platform=None: True)
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "C:/tools/pwsh.exe" if name == "pwsh" else None,
        )

        _launch_windows(["claude"], "C:/repo")

        [launch] = recording_runner.launches
        assert launch.argv[0] == "C:/tools/pwsh.exe"

    def test_macos_wraps_argv_in_osascript_and_strips_claude_env(
        self, monkeypatch, recording_runner,
    ):
        from work_buddy.process import policy
        from work_buddy.process.policy import Intent
        from work_buddy.session_launcher import _launch_macos

        monkeypatch.setattr(policy, "is_windows", lambda platform=None: False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

        pid = _launch_macos(["claude", "hello there"], "/Users/me/repo")

        [launch] = recording_runner.launches
        assert launch.intent is Intent.VISIBLE_TERMINAL
        assert launch.argv[0] == "osascript"
        assert launch.argv[1] == "-e"
        assert "Terminal" in launch.argv[2]
        assert "/Users/me/repo" in launch.argv[2]
        assert "ANTHROPIC_API_KEY" not in launch.env
        assert pid == recording_runner.processes[0].pid

    def test_linux_prefers_gnome_terminal_when_available(self, monkeypatch, recording_runner):
        from work_buddy.process import policy
        from work_buddy.process.policy import Intent
        from work_buddy.session_launcher import _launch_linux

        monkeypatch.setattr(policy, "is_windows", lambda platform=None: False)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

        pid = _launch_linux(["claude", "hello"], "/home/me/repo")

        [launch] = recording_runner.launches
        assert launch.intent is Intent.VISIBLE_TERMINAL
        assert launch.argv[0] == "gnome-terminal"
        assert launch.argv[1] == "--working-directory=/home/me/repo"
        assert pid == recording_runner.processes[0].pid

    def test_linux_falls_back_to_the_next_emulator(self, monkeypatch, recording_runner):
        from work_buddy.process import policy
        from work_buddy.session_launcher import _launch_linux

        monkeypatch.setattr(policy, "is_windows", lambda platform=None: False)
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/xterm" if name == "xterm" else None,
        )

        _launch_linux(["claude", "hello"], "/home/me/repo")

        [launch] = recording_runner.launches
        assert launch.argv[0] == "xterm"
        assert launch.argv[1] == "-e"

    def test_linux_raises_when_no_emulator_found(self, monkeypatch, recording_runner):
        from work_buddy.session_launcher import _launch_linux

        monkeypatch.setattr("shutil.which", lambda name: None)

        with pytest.raises(RuntimeError, match="No terminal emulator found"):
            _launch_linux(["claude", "hello"], "/home/me/repo")

        assert recording_runner.launches == []
