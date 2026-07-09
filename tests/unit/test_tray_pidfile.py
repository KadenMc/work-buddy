"""Tray pid file: single-instance guard + the withdraw-as-graceful-stop signal."""

from __future__ import annotations

import os

import pytest

from work_buddy.tray import pidfile


@pytest.fixture(autouse=True)
def _tmp_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pidfile, "TRAY_PID_FILE", tmp_path / "tray.pid")


class TestCheckExistingTray:
    def test_absent(self):
        assert pidfile.check_existing_tray() is None

    def test_corrupt_removed(self):
        pidfile.TRAY_PID_FILE.write_text("not-a-pid")
        assert pidfile.check_existing_tray() is None
        assert not pidfile.TRAY_PID_FILE.exists()

    def test_live_pid_returned(self):
        pidfile.TRAY_PID_FILE.write_text(f"{os.getpid()}\n")
        assert pidfile.check_existing_tray() == os.getpid()

    def test_stale_pid_cleaned(self, monkeypatch):
        pidfile.TRAY_PID_FILE.write_text("4242\n")
        monkeypatch.setattr(pidfile, "is_process_alive", lambda pid: False)
        assert pidfile.check_existing_tray() is None
        assert not pidfile.TRAY_PID_FILE.exists()


class TestOwnershipAndWithdraw:
    def test_write_then_owns(self):
        pidfile.write_pid_file()
        assert pidfile.TRAY_PID_FILE.read_text().strip() == str(os.getpid())
        assert pidfile.owns_pid_file()

    def test_foreign_pid_not_owned(self):
        pidfile.TRAY_PID_FILE.write_text("4242\n")
        assert not pidfile.owns_pid_file()

    def test_missing_not_owned(self):
        assert not pidfile.owns_pid_file()

    def test_withdraw_removes_regardless_of_owner(self):
        pidfile.TRAY_PID_FILE.write_text("4242\n")
        pidfile.withdraw()
        assert not pidfile.TRAY_PID_FILE.exists()

    def test_cleanup_is_ownership_guarded(self):
        # A successor's file must survive our atexit cleanup.
        pidfile.TRAY_PID_FILE.write_text("4242\n")
        pidfile.cleanup_pid_file()
        assert pidfile.TRAY_PID_FILE.exists()

    def test_cleanup_removes_own_file(self):
        pidfile.write_pid_file()
        pidfile.cleanup_pid_file()
        assert not pidfile.TRAY_PID_FILE.exists()
