"""Focused tests for the sidecar's React build freshness guard."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from work_buddy.dashboard import build_freshness as freshness


def _valid_index(label: str = "test") -> str:
    return f"<!doctype html><html><body>{label}</body></html>"


def _make_dev_checkout(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "dashboard-react"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.tsx").write_text("export const value = 1;\n")
    (root / "public").mkdir()
    (root / "public" / "manifest.webmanifest").write_text("{}\n")
    for name in (
        "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json",
    ):
        (root / name).write_text(f"{name}\n")
    (root / "index.html").write_text(_valid_index("source"))
    monkeypatch.setattr(freshness.paths, "asset_root", lambda: tmp_path)
    return root


def _fake_successful_build(command, *_args, **_kwargs):
    staging = Path(command[command.index("--outDir") + 1])
    (staging / "index.html").write_text(_valid_index("fresh"))
    (staging / "assets").mkdir()
    (staging / "assets" / "app.js").write_text("// built\n")
    return subprocess.CompletedProcess(command, 0, "built", "")


def test_packaged_dist_skips_npm(tmp_path, monkeypatch):
    root = tmp_path / "dashboard-react"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "index.html").write_text(_valid_index("packaged"))
    monkeypatch.setattr(freshness.paths, "asset_root", lambda: tmp_path)
    monkeypatch.setattr(
        freshness, "_npm_executable",
        lambda: (_ for _ in ()).throw(AssertionError("npm must not be queried")),
    )

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.PACKAGED_DIST
    assert result.ready


def test_packaged_dist_with_missing_referenced_asset_is_rejected(
    tmp_path, monkeypatch,
):
    root = tmp_path / "dashboard-react"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "index.html").write_text(
        '<!doctype html><html><script src="/app/assets/missing.js"></script></html>'
    )
    monkeypatch.setattr(freshness.paths, "asset_root", lambda: tmp_path)

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.MISSING_PACKAGED_DIST


def test_partial_development_checkout_fails_closed_instead_of_trusting_dist(
    tmp_path, monkeypatch,
):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    (root / "package-lock.json").unlink()
    (root / "dist").mkdir()
    (root / "dist" / "index.html").write_text(_valid_index("stale"))
    monkeypatch.setattr(
        freshness, "_npm_executable",
        lambda: (_ for _ in ()).throw(AssertionError("npm must not run")),
    )

    result = freshness.ensure_dashboard_react_build()

    assert (
        result.status
        is freshness.DashboardBuildStatus.INCOMPLETE_DEVELOPMENT_CHECKOUT
    )
    assert "package-lock.json" in result.message


def test_authoring_files_without_src_are_not_misclassified_as_packaged(
    tmp_path, monkeypatch,
):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    (root / "src" / "main.tsx").unlink()
    (root / "src").rmdir()
    (root / "dist").mkdir()
    (root / "dist" / "index.html").write_text(_valid_index("stale"))

    result = freshness.ensure_dashboard_react_build()

    assert (
        result.status
        is freshness.DashboardBuildStatus.INCOMPLETE_DEVELOPMENT_CHECKOUT
    )
    assert "src/" in result.message


def test_current_versioned_marker_skips_rebuild(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(freshness, "_npm_executable", lambda: "npm")
    monkeypatch.setattr(
        freshness, "_run_build_with_heartbeats", _fake_successful_build,
    )
    assert freshness.ensure_dashboard_react_build().status is freshness.DashboardBuildStatus.BUILT
    monkeypatch.setattr(
        freshness, "_run_build_with_heartbeats",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected rebuild")),
    )

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.CURRENT_MARKER
    assert (root / "dist" / freshness._BUILD_MARKER_NAME).is_file()


def test_corrupted_emitted_asset_invalidates_otherwise_current_marker(
    tmp_path, monkeypatch,
):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(freshness, "_npm_executable", lambda: "npm")
    monkeypatch.setattr(
        freshness, "_run_build_with_heartbeats", _fake_successful_build,
    )
    assert freshness.ensure_dashboard_react_build().ready
    (root / "dist" / "assets" / "app.js").write_text("// corrupted\n")
    builds: list[str] = []

    def rebuild(command, *_args, **_kwargs):
        builds.append("built")
        return _fake_successful_build(command)

    monkeypatch.setattr(freshness, "_run_build_with_heartbeats", rebuild)

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.BUILT
    assert builds == ["built"]


def test_missing_marker_rebuilds_and_atomically_replaces_dist(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    (root / "dist").mkdir()
    (root / "dist" / "index.html").write_text(_valid_index("last-good"))
    (root / "dist" / "old-only.txt").write_text("old")
    calls: list[list[str]] = []

    def build(command, dashboard_root, *_args, **_kwargs):
        calls.append(command)
        assert dashboard_root == root
        return _fake_successful_build(command)

    monkeypatch.setattr(freshness, "_npm_executable", lambda: "npm")
    monkeypatch.setattr(freshness, "_run_build_with_heartbeats", build)

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.BUILT
    assert calls and "--outDir" in calls[0]
    assert "fresh" in (root / "dist" / "index.html").read_text()
    assert not (root / "dist" / "old-only.txt").exists()
    assert not list(root.glob(f"{freshness._STAGING_PREFIX}*"))


def test_stale_sources_without_npm_write_error_and_preserve_dist(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    (root / "dist").mkdir()
    old_index = _valid_index("last-good")
    (root / "dist" / "index.html").write_text(old_index)
    monkeypatch.setattr(freshness, "_npm_executable", lambda: None)

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.NPM_UNAVAILABLE
    assert (root / "dist" / "index.html").read_text() == old_index
    error = freshness.read_dashboard_build_error()
    assert error is not None and error.status == "npm_unavailable"


def test_failed_build_preserves_last_good_dist(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    (root / "dist").mkdir()
    old_index = _valid_index("last-good")
    (root / "dist" / "index.html").write_text(old_index)
    monkeypatch.setattr(freshness, "_npm_executable", lambda: "npm")
    monkeypatch.setattr(
        freshness, "_run_build_with_heartbeats",
        lambda command, *_args, **_kwargs: subprocess.CompletedProcess(
            command, 2, "", "typescript failed",
        ),
    )

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.BUILD_FAILED
    assert "typescript failed" in (result.diagnostic or "")
    assert "typescript failed" not in result.message
    assert (root / "dist" / "index.html").read_text() == old_index


def test_source_change_during_build_is_not_installed(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    (root / "dist").mkdir()
    old_index = _valid_index("last-good")
    (root / "dist" / "index.html").write_text(old_index)
    monkeypatch.setattr(freshness, "_npm_executable", lambda: "npm")

    def racing_build(command, *_args, **_kwargs):
        result = _fake_successful_build(command)
        (root / "src" / "main.tsx").write_text("export const value = 2;\n")
        return result

    monkeypatch.setattr(freshness, "_run_build_with_heartbeats", racing_build)

    result = freshness.ensure_dashboard_react_build()

    assert result.status is freshness.DashboardBuildStatus.INPUTS_CHANGED
    assert (root / "dist" / "index.html").read_text() == old_index


def test_build_runner_publishes_boot_heartbeat(tmp_path, monkeypatch):
    _make_dev_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(freshness, "_npm_executable", lambda: "npm")
    def build_with_beat(command, _root, _timeout, heartbeat, *_args):
        if heartbeat is not None:
            heartbeat()
        return _fake_successful_build(command)

    monkeypatch.setattr(
        freshness, "_run_build_with_heartbeats", build_with_beat,
    )
    beats: list[str] = []

    result = freshness.ensure_dashboard_react_build(
        heartbeat=lambda: beats.append("beat"),
        heartbeat_interval_seconds=0.01,
    )

    assert result.ready
    assert beats


def test_build_timeout_is_typed_and_preserves_last_good_dist(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    (root / "dist").mkdir()
    old_index = _valid_index("last-good")
    (root / "dist" / "index.html").write_text(old_index)
    monkeypatch.setattr(freshness, "_npm_executable", lambda: "npm")
    monkeypatch.setattr(
        freshness,
        "_run_build_with_heartbeats",
        lambda command, *_args: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(command, 1),
        ),
    )

    result = freshness.ensure_dashboard_react_build(timeout_seconds=1)

    assert result.status is freshness.DashboardBuildStatus.BUILD_TIMED_OUT
    assert (root / "dist" / "index.html").read_text() == old_index


def test_build_runner_reaps_owned_process_when_shutdown_is_requested(
    tmp_path, monkeypatch,
):
    root = tmp_path / "dashboard-react"
    root.mkdir()
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    def terminate(owned):
        captured["terminated"] = owned.pid
        owned.returncode = -1

    monkeypatch.setattr(freshness.subprocess, "Popen", popen)
    monkeypatch.setattr(freshness, "_terminate_build_process", terminate)
    started: list[int] = []

    with pytest.raises(freshness._BuildCancelled):
        freshness._run_build_with_heartbeats(
            ["npm", "run", "build"],
            root,
            10,
            None,
            1,
            lambda: True,
            started.append,
        )

    assert started == [4321]
    assert captured["terminated"] == 4321
    assert captured["kwargs"]["shell"] is False


def test_build_runner_reaps_process_when_start_callback_raises(
    tmp_path, monkeypatch,
):
    root = tmp_path / "dashboard-react"
    root.mkdir()
    terminated: list[int] = []

    class FakeProcess:
        pid = 9876
        returncode = None

        def poll(self):
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(
        freshness.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )

    def terminate(owned):
        terminated.append(owned.pid)
        owned.returncode = -1

    monkeypatch.setattr(freshness, "_terminate_build_process", terminate)

    with pytest.raises(RuntimeError, match="job assignment failed"):
        freshness._run_build_with_heartbeats(
            ["npm", "run", "build"],
            root,
            10,
            None,
            1,
            None,
            lambda _pid: (_ for _ in ()).throw(
                RuntimeError("job assignment failed"),
            ),
        )

    assert terminated == [9876]


def test_interrupted_swap_recovery_restores_last_good_backup(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    backup = root / f"{freshness._BACKUP_PREFIX}123"
    backup.mkdir()
    (backup / "index.html").write_text(_valid_index("recovered"))
    staging = root / f"{freshness._STAGING_PREFIX}123"
    staging.mkdir()
    (staging / "partial.txt").write_text("partial")

    freshness._recover_generated_trees(root, root / "dist")

    assert "recovered" in (root / "dist" / "index.html").read_text()
    assert not backup.exists()
    assert not staging.exists()


def test_concurrent_build_lock_times_out_without_running_npm(tmp_path, monkeypatch):
    root = _make_dev_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(
        freshness, "_npm_executable",
        lambda: (_ for _ in ()).throw(AssertionError("npm must not run")),
    )

    with freshness._dashboard_build_lock(
        root, timeout_seconds=1, heartbeat=None,
    ):
        result = freshness.ensure_dashboard_react_build(
            lock_timeout_seconds=0.01,
        )

    assert result.status is freshness.DashboardBuildStatus.BUILD_LOCK_TIMED_OUT


def test_sidecar_runs_preflight_once_without_disabling_dashboard(
    tmp_path, monkeypatch, caplog,
):
    from work_buddy.sidecar import daemon
    from work_buddy.sidecar.state import SidecarState, ServiceHealth

    dashboard = daemon.ChildService(
        name="dashboard", module="work_buddy.dashboard", port=5127,
    )
    messaging = daemon.ChildService(
        name="messaging", module="work_buddy.messaging.service", port=5123,
    )
    state = SidecarState()
    state.services["dashboard"] = ServiceHealth("dashboard", 5127, "starting")
    killed: list[tuple[int, str]] = []
    heartbeats: list[float] = []
    monkeypatch.setattr(
        daemon, "_kill_process_on_port",
        lambda port, *, service_name="": killed.append((port, service_name)) or True,
    )
    monkeypatch.setattr(
        daemon, "save_state",
        lambda current: heartbeats.append(current.last_tick_at),
    )
    failure = freshness.DashboardBuildResult(
        status=freshness.DashboardBuildStatus.BUILD_FAILED,
        message="TypeScript failed",
        dashboard_root=tmp_path / "dashboard-react",
        dist_root=tmp_path / "dashboard-react" / "dist",
    )
    monkeypatch.setattr(
        freshness, "ensure_dashboard_react_build",
        lambda **kwargs: failure,
    )

    result = daemon._preflight_dashboard_react_build(
        [dashboard, messaging], state,
    )

    assert result is failure
    assert killed == [(5127, "dashboard")]
    assert heartbeats
    assert dashboard.enabled is True  # Flask still launches; /app alone is 503.
    assert dashboard.environment[freshness._BUILD_STATE_ENV] == "build_failed"


def test_sidecar_preflight_exception_fails_app_closed_but_does_not_escape(
    tmp_path, monkeypatch,
):
    from work_buddy.sidecar import daemon
    from work_buddy.sidecar.state import SidecarState, ServiceHealth

    dashboard = daemon.ChildService(
        name="dashboard", module="work_buddy.dashboard", port=5127,
    )
    state = SidecarState()
    state.services["dashboard"] = ServiceHealth("dashboard", 5127, "starting")
    monkeypatch.setattr(
        daemon, "_kill_process_on_port", lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(daemon, "save_state", lambda _state: None)
    monkeypatch.setattr(
        freshness, "ensure_dashboard_react_build",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    monkeypatch.setattr(
        freshness, "record_dashboard_build_error", lambda *_args, **_kwargs: None,
    )

    result = daemon._preflight_dashboard_react_build([dashboard], state)

    assert result is None
    assert dashboard.environment[freshness._BUILD_STATE_ENV] == "internal_error"


def test_sidecar_port_conflict_skips_build_and_fails_app_closed(
    tmp_path, monkeypatch,
):
    from work_buddy.sidecar import daemon
    from work_buddy.sidecar.state import SidecarState, ServiceHealth

    dashboard = daemon.ChildService(
        name="dashboard", module="work_buddy.dashboard", port=5127,
    )
    state = SidecarState()
    state.services["dashboard"] = ServiceHealth("dashboard", 5127, "starting")
    monkeypatch.setattr(
        daemon, "_kill_process_on_port", lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(daemon, "save_state", lambda _state: None)
    monkeypatch.setattr(
        freshness, "ensure_dashboard_react_build",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("build must not run while the port is owned"),
        ),
    )
    monkeypatch.setattr(
        freshness, "record_dashboard_build_error", lambda *_args, **_kwargs: None,
    )

    result = daemon._preflight_dashboard_react_build([dashboard], state)

    assert result is not None
    assert result.status is freshness.DashboardBuildStatus.DASHBOARD_PORT_BUSY
    assert dashboard.environment[freshness._BUILD_STATE_ENV] == "dashboard_port_busy"
