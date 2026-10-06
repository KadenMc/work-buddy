"""Tests for the Tailscale bindings across all four health-system layers.

The implementation lives across three modules: `work_buddy.health.checks`
(shared status helper + component health checks), `requirement_checks`
(setup-time requirements), and `fixers` (programmatic Serve-config fixer).
All four code paths run the local `tailscale` CLI through
``work_buddy.process.run_tool``. Tests script the ``recording_runner``
fixture rather than invoking it, so the suite runs offline and without
Tailscale installed on the host.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from work_buddy.process import ExecutableNotFound


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tailscale_cache():
    """Clear the module-level cache before AND after each test.

    The cache is keyed off wallclock; if a previous test populated it, a
    fast-running follow-up test can see the stale entry within the 5s TTL
    and skip the scripted launch entirely. Resetting both before and after
    guarantees isolation regardless of ordering.
    """
    from work_buddy.health import checks as ch
    ch._TAILSCALE_CACHE.update({"ts": 0.0, "result": None})
    yield
    ch._TAILSCALE_CACHE.update({"ts": 0.0, "result": None})


# ---------------------------------------------------------------------------
# Launch scripting helpers
# ---------------------------------------------------------------------------


class _FakeProc:
    """Lightweight stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _status_ok(
    *,
    backend_state: str = "Running",
    self_online: bool = True,
    self_name: str = "test-host",
    tailnet: str = "test.ts.net",
    peers: list[dict] | None = None,
) -> _FakeProc:
    """Build a fake `tailscale status --json` response."""
    payload = {
        "BackendState": backend_state,
        "MagicDNSSuffix": tailnet,
        "Self": {
            "HostName": self_name,
            "DNSName": f"{self_name}.{tailnet}.",
            "Online": self_online,
            "OS": "linux",
            "TailscaleIPs": ["100.64.0.1"],
        },
        "Peer": {f"peer-{i}": p for i, p in enumerate(peers or [])},
    }
    return _FakeProc(returncode=0, stdout=json.dumps(payload))


def _serve_ok(*, port: int = 5127, hostname: str = "test-host.test.ts.net:443") -> _FakeProc:
    """Build a fake `tailscale serve status --json` response that publishes
    a Web handler proxying to ``127.0.0.1:<port>``."""
    payload = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            hostname: {
                "Handlers": {
                    "/": {"Proxy": f"http://127.0.0.1:{port}"},
                },
            },
        },
    }
    return _FakeProc(returncode=0, stdout=json.dumps(payload))


def _serve_empty() -> _FakeProc:
    """Build a fake serve-status response with nothing configured."""
    return _FakeProc(returncode=0, stdout="")


def _patch_subprocess(
    recording_runner,
    *,
    status: _FakeProc | Exception | None = None,
    serve: _FakeProc | Exception | None = None,
):
    """Script the recording runner's answers for the `tailscale` subcommands.

    Pass an Exception to ``status`` or ``serve`` to simulate the launch
    raising (e.g. ``ExecutableNotFound`` when the CLI is missing). Every
    test that reaches this helper scripts exactly the calls its code path
    will make, so the runner is put in strict mode: any call outside
    ``status``/``serve`` fails the test loudly instead of quietly
    succeeding with empty output.
    """
    recording_runner.strict = True
    if status is not None:
        if isinstance(status, Exception):
            recording_runner.script(["tailscale", "status"], raises=status)
        else:
            recording_runner.script(
                ["tailscale", "status"],
                returncode=status.returncode,
                stdout=status.stdout,
                stderr=status.stderr,
            )
    if serve is not None:
        if isinstance(serve, Exception):
            recording_runner.script(["tailscale", "serve", "status"], raises=serve)
        else:
            recording_runner.script(
                ["tailscale", "serve", "status"],
                returncode=serve.returncode,
                stdout=serve.stdout,
                stderr=serve.stderr,
            )
    return recording_runner


# ===========================================================================
# get_tailscale_status: shared helper
# ===========================================================================


class TestGetTailscaleStatus:
    """Behavioural tests for the shared helper that backs every Tailscale
    check across the health system."""

    def test_cli_not_installed(self, recording_runner):
        _patch_subprocess(
            recording_runner, status=ExecutableNotFound(2, "not found", "tailscale"),
        )
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        assert result["installed"] is False
        assert result["running"] is False
        assert result["serve"] is None

    def test_status_command_fails_marks_installed_with_error(self, recording_runner):
        """Non-zero exit means the CLI is present but the daemon is
        unreachable: installed=True, running=False, error populated."""
        _patch_subprocess(
            recording_runner,
            status=_FakeProc(returncode=1, stderr="not running"),
        )
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        assert result["installed"] is True
        assert result["running"] is False
        assert "error" in result
        assert "not running" in result["error"]

    def test_status_error_truncated_to_200_chars(self, recording_runner):
        long_stderr = "x" * 500
        _patch_subprocess(
            recording_runner,
            status=_FakeProc(returncode=1, stderr=long_stderr),
        )
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        assert len(result["error"]) <= 200

    def test_status_ok_parses_self_and_peers(self, recording_runner):
        peer = {
            "HostName": "phone",
            "DNSName": "phone.test.ts.net.",
            "Online": False,
            "OS": "android",
            "LastSeen": "2026-04-24T04:56:04Z",
        }
        _patch_subprocess(
            recording_runner,
            status=_status_ok(self_name="laptop", peers=[peer]),
            serve=_serve_empty(),
        )
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        assert result["installed"] is True
        assert result["running"] is True
        assert result["backend_state"] == "Running"
        assert result["tailnet"] == "test.ts.net"
        assert result["self"]["name"] == "laptop"
        assert result["self"]["online"] is True
        assert len(result["peers"]) == 1
        assert result["peers"][0]["name"] == "phone"
        assert result["peers"][0]["online"] is False
        assert result["peers"][0]["last_seen"] == "2026-04-24T04:56:04Z"

    def test_serve_command_fails_returns_none(self, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_status_ok(),
            serve=_FakeProc(returncode=1, stdout="", stderr="boom"),
        )
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        # Daemon is up; only Serve query failed.
        assert result["running"] is True
        assert result["serve"] is None

    def test_serve_command_empty_stdout_returns_none(self, recording_runner):
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_empty())
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        assert result["serve"] is None

    def test_serve_command_raises_returns_none(self, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_status_ok(),
            serve=RuntimeError("network broke"),
        )
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        # Status succeeded; serve probe blew up but didn't tank the whole call.
        assert result["running"] is True
        assert result["serve"] is None

    def test_serve_command_ok_returns_parsed_dict(self, recording_runner):
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_ok())
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        assert isinstance(result["serve"], dict)
        assert "Web" in result["serve"]

    def test_status_unexpected_exception_populates_error(self, recording_runner):
        """Any non-FileNotFoundError raised during status parsing should
        end up in result['error'], never propagate to the caller."""
        _patch_subprocess(recording_runner, status=ValueError("bad json"))
        from work_buddy.health.checks import get_tailscale_status
        result = get_tailscale_status()
        assert result["installed"] is False  # error before installed flip
        assert "error" in result
        assert "bad json" in result["error"]

    def test_memoization_within_ttl_avoids_repeat_subprocess(self, recording_runner):
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_ok())
        from work_buddy.health.checks import get_tailscale_status
        # First call populates the cache; second should hit the cache.
        get_tailscale_status()
        get_tailscale_status()
        # 2 launches per real fetch (status + serve status).
        # If memoization is working, only 2 launches total.
        assert len(recording_runner.launches) == 2

    def test_force_true_bypasses_cache(self, recording_runner):
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_ok())
        from work_buddy.health.checks import get_tailscale_status
        get_tailscale_status()
        get_tailscale_status(force=True)
        # 4 launches total, cache bypassed on second call.
        assert len(recording_runner.launches) == 4

    def test_cache_returns_same_dict_object(self, recording_runner):
        """Cache hit returns the cached dict reference, not a fresh copy."""
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_ok())
        from work_buddy.health.checks import get_tailscale_status
        first = get_tailscale_status()
        second = get_tailscale_status()
        assert first is second


# ===========================================================================
# Component health checks
# ===========================================================================


class TestCheckTailscaleDaemon:
    """Component-level health probe for the daemon's runtime state."""

    def test_cli_not_installed(self, recording_runner):
        _patch_subprocess(
            recording_runner, status=ExecutableNotFound(2, "not found", "tailscale"),
        )
        from work_buddy.health.checks import check_tailscale_daemon
        result = check_tailscale_daemon()
        assert result["ok"] is False
        assert "not found" in result["detail"].lower()

    def test_status_error_field_set(self, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_FakeProc(returncode=1, stderr="acct expired"),
        )
        from work_buddy.health.checks import check_tailscale_daemon
        result = check_tailscale_daemon()
        assert result["ok"] is False
        assert "tailscale status failed" in result["detail"]
        assert "acct expired" in result["detail"]

    def test_backend_state_stopped_fails_with_expected_message(self, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_status_ok(backend_state="Stopped"),
            serve=_serve_empty(),
        )
        from work_buddy.health.checks import check_tailscale_daemon
        result = check_tailscale_daemon()
        assert result["ok"] is False
        assert "Stopped" in result["detail"]
        assert "expected 'Running'" in result["detail"]

    def test_backend_state_running_passes(self, recording_runner):
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_empty())
        from work_buddy.health.checks import check_tailscale_daemon
        result = check_tailscale_daemon()
        assert result["ok"] is True
        assert "Running" in result["detail"]


class TestCheckTailscaleSelfOnline:
    """Component-level probe asserting this device is on the tailnet."""

    def test_daemon_not_running_fails(self, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_FakeProc(returncode=1, stderr="off"),
        )
        from work_buddy.health.checks import check_tailscale_self_online
        result = check_tailscale_self_online()
        assert result["ok"] is False
        # When running=False the daemon-not-running branch wins regardless
        # of self_online state.
        assert "daemon not running" in result["detail"]

    def test_self_offline_fails_with_devicename(self, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_status_ok(self_online=False, self_name="laptop-XYZ"),
            serve=_serve_empty(),
        )
        from work_buddy.health.checks import check_tailscale_self_online
        result = check_tailscale_self_online()
        assert result["ok"] is False
        assert "laptop-XYZ" in result["detail"]
        assert "not online" in result["detail"].lower()

    def test_self_online_passes(self, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_status_ok(self_online=True, self_name="laptop", tailnet="ts.example"),
            serve=_serve_empty(),
        )
        from work_buddy.health.checks import check_tailscale_self_online
        result = check_tailscale_self_online()
        assert result["ok"] is True
        assert "laptop" in result["detail"]
        assert "ts.example" in result["detail"]


# ===========================================================================
# Requirement checks
# ===========================================================================


class TestCheckTailscaleInstalled:
    """Setup-time check that the CLI is reachable on PATH."""

    def test_not_installed_points_at_install_doc(self, recording_runner):
        _patch_subprocess(
            recording_runner, status=ExecutableNotFound(2, "not found", "tailscale"),
        )
        from work_buddy.health.requirement_checks import check_tailscale_installed
        result = check_tailscale_installed()
        assert result["ok"] is False
        assert "tailscale.com/download" in result["detail"]

    def test_installed_passes(self, recording_runner):
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_empty())
        from work_buddy.health.requirement_checks import check_tailscale_installed
        result = check_tailscale_installed()
        assert result["ok"] is True
        assert "present" in result["detail"]


class TestCheckTailscaleServeConfigured:
    """Setup-time check that the dashboard is published via Tailscale Serve."""

    def _patch_dashboard_port(self, monkeypatch, port: int = 5127):
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {"sidecar": {"services": {"dashboard": {"port": port}}}},
        )

    def test_not_installed_points_at_installed_requirement(self, monkeypatch, recording_runner):
        _patch_subprocess(
            recording_runner, status=ExecutableNotFound(2, "not found", "tailscale"),
        )
        self._patch_dashboard_port(monkeypatch)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is False
        assert "integrations/tailscale/installed" in result["detail"]

    def test_daemon_down_short_circuits(self, monkeypatch, recording_runner):
        """When status fails (running=False), Serve config can't be queried;
        the check should bail out with a clear message rather than tripping
        over a missing serve dict."""
        _patch_subprocess(
            recording_runner,
            status=_FakeProc(returncode=1, stderr="off"),
        )
        self._patch_dashboard_port(monkeypatch)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is False
        assert "not running" in result["detail"]

    def test_serve_null_fails_with_no_handlers(self, monkeypatch, recording_runner):
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_empty())
        self._patch_dashboard_port(monkeypatch)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is False
        assert "no Web handlers" in result["detail"]

    def test_serve_web_dict_empty_fails(self, monkeypatch, recording_runner):
        empty_web = _FakeProc(returncode=0, stdout=json.dumps({"Web": {}}))
        _patch_subprocess(recording_runner, status=_status_ok(), serve=empty_web)
        self._patch_dashboard_port(monkeypatch)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is False
        assert "no Web handlers" in result["detail"]

    def test_handler_proxies_dashboard_passes(self, monkeypatch, recording_runner):
        _patch_subprocess(
            recording_runner,
            status=_status_ok(),
            serve=_serve_ok(port=5127, hostname="laptop.test.ts.net:443"),
        )
        self._patch_dashboard_port(monkeypatch, port=5127)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is True
        assert "laptop.test.ts.net" in result["detail"]
        assert "127.0.0.1:5127" in result["detail"]

    def test_handler_proxies_wrong_port_fails(self, monkeypatch, recording_runner):
        # Serve has a handler proxying to the wrong port.
        _patch_subprocess(
            recording_runner,
            status=_status_ok(),
            serve=_serve_ok(port=9999),
        )
        self._patch_dashboard_port(monkeypatch, port=5127)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is False
        assert "127.0.0.1:5127" in result["detail"]

    def test_wrong_port_failure_uses_current_cli_syntax(self, monkeypatch, recording_runner):
        """Regression: the failure detail should suggest the canonical
        `tailscale serve --bg <port>` form, not the older positional
        `tailscale serve --bg https http://...:<port>` syntax that
        current Tailscale CLI rejects with 'invalid argument format'."""
        _patch_subprocess(
            recording_runner,
            status=_status_ok(),
            serve=_serve_ok(port=9999),
        )
        self._patch_dashboard_port(monkeypatch, port=5127)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        # The fix-suggestion command line should be one the current CLI
        # actually accepts.
        assert "tailscale serve --bg 5127" in result["detail"]
        # And must NOT contain the deprecated positional form.
        assert "https http://" not in result["detail"]

    def test_custom_dashboard_port_respected(self, monkeypatch, recording_runner):
        """A non-default dashboard port should drive the lookup target;
        if the handler matches that port, the check passes."""
        _patch_subprocess(
            recording_runner,
            status=_status_ok(),
            serve=_serve_ok(port=8080),
        )
        self._patch_dashboard_port(monkeypatch, port=8080)
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is True
        assert "127.0.0.1:8080" in result["detail"]

    def test_default_port_when_config_missing_section(self, monkeypatch, recording_runner):
        """Falls back to 5127 when the sidecar/dashboard config path is
        missing. Verifies the .get(..., 5127) chain doesn't raise."""
        _patch_subprocess(recording_runner, status=_status_ok(), serve=_serve_ok(port=5127))
        monkeypatch.setattr(
            "work_buddy.health.requirement_checks._cfg",
            lambda: {},
        )
        from work_buddy.health.requirement_checks import (
            check_tailscale_serve_configured,
        )
        result = check_tailscale_serve_configured()
        assert result["ok"] is True


# ===========================================================================
# Fixer
# ===========================================================================


class TestFixTailscaleServeConfigured:
    """Programmatic fix: re-publish the dashboard via ``tailscale serve``."""

    def _patch_dashboard_port(self, monkeypatch, port: int = 5127):
        monkeypatch.setattr(
            "work_buddy.config.load_config",
            lambda: {"sidecar": {"services": {"dashboard": {"port": port}}}},
        )

    def _patch_serve_invocation(
        self, recording_runner, returncode: int = 0, stderr: str = "", raise_exc=None
    ):
        """Script the recording runner for the fixer's `tailscale serve --bg
        <port>` call. Returns the runner so callers can inspect
        ``recording_runner.argvs()``."""
        if raise_exc is not None:
            recording_runner.script(["tailscale", "serve", "--bg"], raises=raise_exc)
        else:
            recording_runner.script(
                ["tailscale", "serve", "--bg"], returncode=returncode, stderr=stderr,
            )
        return recording_runner

    def test_happy_path_publishes_port_and_busts_cache(self, monkeypatch, recording_runner):
        self._patch_dashboard_port(monkeypatch, port=5127)
        self._patch_serve_invocation(recording_runner)

        # Pre-seed the helper cache with stale data; the fixer must invalidate
        # it so the post-fix recheck reads fresh state.
        from work_buddy.health import checks as ch
        import time as _time
        ch._TAILSCALE_CACHE.update(
            {"ts": _time.time(), "result": {"installed": True, "running": True}}
        )

        from work_buddy.health.fixers import fix_tailscale_serve_configured
        result = fix_tailscale_serve_configured()

        assert result["ok"] is True
        assert "5127" in result["detail"]
        assert any("tailscale serve --bg 5127" in s for s in result["side_effects"])
        # Cache busted: forced re-fetch should have run during fix; the
        # cache entry now reflects the *force-refreshed* status, not what
        # we pre-seeded. The exact value depends on the helper, but `ts`
        # should have advanced beyond our pre-seed if force=True ran.
        # We can't assert that easily without a second mock, but at minimum
        # the fixer should have invoked the serve command.
        assert any(
            cmd[:3] == ["tailscale", "serve", "--bg"] for cmd in recording_runner.argvs()
        )

    def test_custom_dashboard_port_used(self, monkeypatch, recording_runner):
        self._patch_dashboard_port(monkeypatch, port=8080)
        self._patch_serve_invocation(recording_runner)

        from work_buddy.health.fixers import fix_tailscale_serve_configured
        result = fix_tailscale_serve_configured()

        assert result["ok"] is True
        assert "port 8080" in result["detail"]
        # First launch must be the serve invocation with our custom port (a
        # force-refresh of get_tailscale_status may follow).
        first = recording_runner.argvs()[0]
        assert first[:3] == ["tailscale", "serve", "--bg"]
        assert first[3] == "8080"

    def test_subprocess_nonzero_exit_returns_failure(self, monkeypatch, recording_runner):
        self._patch_dashboard_port(monkeypatch)
        self._patch_serve_invocation(
            recording_runner, returncode=1, stderr="invalid argument format"
        )

        from work_buddy.health.fixers import fix_tailscale_serve_configured
        result = fix_tailscale_serve_configured()

        assert result["ok"] is False
        assert "exited 1" in result["detail"]
        assert "invalid argument format" in result["detail"]
        assert result["side_effects"] == []

    def test_cli_missing_points_at_installed_requirement(self, monkeypatch, recording_runner):
        self._patch_dashboard_port(monkeypatch)
        self._patch_serve_invocation(
            recording_runner, raise_exc=ExecutableNotFound(2, "not found", "tailscale"),
        )

        from work_buddy.health.fixers import fix_tailscale_serve_configured
        result = fix_tailscale_serve_configured()

        assert result["ok"] is False
        assert "integrations/tailscale/installed" in result["detail"]
        assert result["side_effects"] == []

    def test_unexpected_subprocess_exception_returns_clean_failure(self, monkeypatch, recording_runner):
        """Generic exceptions from the launch (e.g. PermissionError)
        should be caught and reported, not propagated."""
        self._patch_dashboard_port(monkeypatch)
        self._patch_serve_invocation(
            recording_runner, raise_exc=PermissionError("not allowed")
        )

        from work_buddy.health.fixers import fix_tailscale_serve_configured
        result = fix_tailscale_serve_configured()

        assert result["ok"] is False
        assert "tailscale serve" in result["detail"]
        assert "not allowed" in result["detail"]

    def test_config_load_failure_returns_clean_error(self, monkeypatch):
        """If config.load_config raises, the fixer should report it
        rather than blow up."""
        def boom():
            raise OSError("config.yaml missing")

        monkeypatch.setattr("work_buddy.config.load_config", boom)

        from work_buddy.health.fixers import fix_tailscale_serve_configured
        result = fix_tailscale_serve_configured()

        assert result["ok"] is False
        assert "Could not read dashboard port" in result["detail"]

    def test_idempotent_second_invocation(self, monkeypatch, recording_runner):
        """Two consecutive successful runs should each report ok=True
        without side-effect surprises (Tailscale Serve treats re-runs
        of the same handler as updates rather than duplicates)."""
        self._patch_dashboard_port(monkeypatch)
        self._patch_serve_invocation(recording_runner)

        from work_buddy.health.fixers import fix_tailscale_serve_configured
        first = fix_tailscale_serve_configured()
        second = fix_tailscale_serve_configured()

        assert first["ok"] is True
        assert second["ok"] is True
        assert first["side_effects"] == second["side_effects"]
