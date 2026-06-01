"""Unit tests for ``daemon._roll_oversize_log`` and ``_oversize_service_logs``.

The daemon's child-stdout capture writes to raw OS file handles, and the
child holds that handle open for its whole lifetime, so an oversized log can
only be rolled aside at child *startup* (before the handle exists). The
daemon owns exactly that roll; retention of the rolled-out backups belongs to
the ``service-logs`` artifact reaper, not here. The roll renames an oversized
live log to a unique timestamped backup (``<stem>.<UTC-timestamp>.log``),
mirroring logrotate's ``dateext`` model, and never deletes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from freezegun import freeze_time

from work_buddy.sidecar import daemon

# A backup name looks like ``messaging.20260601T120000123456.log`` (8-digit
# date, T, 6-digit time, 6-digit microseconds) with an optional ``-<n>``
# collision tiebreaker before the ``.log`` suffix.
_BACKUP_RE = re.compile(r"^messaging\.\d{8}T\d{12}(-\d+)?\.log$")


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "messaging.log"


def _write_n_bytes(path: Path, n: int) -> None:
    path.write_bytes(b"x" * n)


# --------------------------------------------------------------------------
# _roll_oversize_log
# --------------------------------------------------------------------------


def test_no_op_when_file_missing(log_path):
    """No file → returns None, no exception, nothing created."""
    assert daemon._roll_oversize_log(log_path) is None
    assert not log_path.exists()


def test_no_op_when_below_cap(log_path):
    """File below cap → untouched, returns None."""
    _write_n_bytes(log_path, 1024)  # 1 KiB << 16 MiB
    assert daemon._roll_oversize_log(log_path) is None
    assert log_path.exists() and log_path.stat().st_size == 1024
    # No backup sibling created.
    assert list(log_path.parent.glob("messaging.*.log")) == []


def test_rolls_when_over_cap(log_path, monkeypatch):
    """Over-cap live log → renamed to a timestamped backup, live slot freed."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    _write_n_bytes(log_path, 2048)  # 2× the cap
    backup = daemon._roll_oversize_log(log_path)
    assert backup is not None
    assert not log_path.exists()  # live slot freed for a fresh log
    assert backup.exists() and backup.stat().st_size == 2048


def test_backup_name_is_timestamped_not_numbered(log_path, monkeypatch):
    """Backups use the dateext scheme, NOT the legacy numbered ``.1.log``."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    _write_n_bytes(log_path, 2048)
    backup = daemon._roll_oversize_log(log_path)
    assert backup is not None
    assert _BACKUP_RE.match(backup.name), backup.name
    # The old numbered convention must not be produced.
    assert not log_path.with_suffix(".1.log").exists()


def test_successive_rolls_do_not_clobber(log_path, monkeypatch):
    """Two rolls (live recreated between) keep two distinct backups."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    _write_n_bytes(log_path, 2048)
    b1 = daemon._roll_oversize_log(log_path)
    _write_n_bytes(log_path, 4096)
    b2 = daemon._roll_oversize_log(log_path)
    assert b1 is not None and b2 is not None
    assert b1 != b2
    assert b1.exists() and b1.stat().st_size == 2048
    assert b2.exists() and b2.stat().st_size == 4096


def test_collision_tiebreaker_under_frozen_clock(log_path, monkeypatch):
    """Identical timestamps (frozen clock) get a ``-<n>`` tiebreaker, no clobber."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    with freeze_time("2026-06-01T12:00:00Z"):
        _write_n_bytes(log_path, 2048)
        b1 = daemon._roll_oversize_log(log_path)
        _write_n_bytes(log_path, 2048)
        b2 = daemon._roll_oversize_log(log_path)
    assert b1 is not None and b2 is not None
    assert b1 != b2
    assert b1.exists() and b2.exists()
    # The second backup carries the counter suffix.
    assert b2.name.endswith("-1.log")


def test_never_deletes_existing_backups(log_path, monkeypatch):
    """A pre-existing (even oversized) backup is left untouched by a roll."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    # Simulate a legacy orphan that the reaper (not the daemon) should clean.
    orphan = log_path.parent / "messaging.1.log"
    orphan.write_bytes(b"legacy-orphan")
    _write_n_bytes(log_path, 2048)
    daemon._roll_oversize_log(log_path)
    assert orphan.exists() and orphan.read_bytes() == b"legacy-orphan"


# --------------------------------------------------------------------------
# _oversize_service_logs
# --------------------------------------------------------------------------


def test_oversize_service_logs_lists_only_over_cap_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    (tmp_path / "messaging.log").write_bytes(b"x" * 2048)
    (tmp_path / "messaging.1.log").write_bytes(b"x" * 4096)
    (tmp_path / "small.log").write_bytes(b"x" * 10)  # under cap
    (tmp_path / "notalog.txt").write_bytes(b"x" * 5000)  # not a .log
    result = {p.name: n for p, n in daemon._oversize_service_logs(tmp_path)}
    assert result == {"messaging.log": 2048, "messaging.1.log": 4096}


def test_oversize_service_logs_missing_dir(tmp_path):
    assert daemon._oversize_service_logs(tmp_path / "does-not-exist") == []


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------


def test_default_cap_is_16_mib():
    """Sanity-check the documented constant value."""
    assert daemon._SERVICE_LOG_CAP_BYTES == 16 * 1024 * 1024
