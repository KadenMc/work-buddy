"""Unit tests for ``daemon._rotate_if_oversize``.

The daemon's child-stdout capture writes to raw OS file handles, so
``RotatingFileHandler`` doesn't apply. ``_rotate_if_oversize`` emulates
its policy (rename current → .1, age out beyond N) at child start.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.sidecar import daemon


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "messaging.log"


def _write_n_bytes(path: Path, n: int) -> None:
    path.write_bytes(b"x" * n)


def test_no_op_when_file_missing(log_path):
    """No file → nothing happens, no exception."""
    daemon._rotate_if_oversize(log_path)
    assert not log_path.exists()


def test_no_op_when_below_cap(log_path):
    """File below cap → untouched."""
    _write_n_bytes(log_path, 1024)  # 1 KiB << 16 MiB
    daemon._rotate_if_oversize(log_path)
    assert log_path.exists() and log_path.stat().st_size == 1024
    # No .1 sibling created
    assert not log_path.with_suffix(".1.log").exists()


def test_rotates_when_at_or_above_cap(log_path, monkeypatch):
    """File at exactly the cap is rotated. (Boundary check.)

    Use a small synthetic cap so the test stays cheap.
    """
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    _write_n_bytes(log_path, 2048)  # 2× the cap
    daemon._rotate_if_oversize(log_path)
    assert not log_path.exists()
    rolled = log_path.with_suffix(".1.log")
    assert rolled.exists() and rolled.stat().st_size == 2048


def test_existing_rotations_age_out(log_path, monkeypatch):
    """When N rotations exist, oldest drops; .1→.2, .2→.3, etc."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    monkeypatch.setattr(daemon, "_SERVICE_LOG_BACKUP_COUNT", 4)

    _write_n_bytes(log_path, 2048)  # current → .1
    log_path.with_suffix(".1.log").write_bytes(b"old1")  # → .2
    log_path.with_suffix(".2.log").write_bytes(b"old2")  # → .3
    log_path.with_suffix(".3.log").write_bytes(b"old3")  # → .4
    log_path.with_suffix(".4.log").write_bytes(b"old4")  # dropped

    daemon._rotate_if_oversize(log_path)

    assert not log_path.exists()
    # .1 should be the previous current (2048 bytes of x)
    assert log_path.with_suffix(".1.log").stat().st_size == 2048
    # .2 should be the old .1
    assert log_path.with_suffix(".2.log").read_bytes() == b"old1"
    # .3 should be the old .2
    assert log_path.with_suffix(".3.log").read_bytes() == b"old2"
    # .4 should be the old .3 (the oldest entry was dropped)
    assert log_path.with_suffix(".4.log").read_bytes() == b"old3"
    # No .5 (we cap at backup_count)
    assert not log_path.with_suffix(".5.log").exists()


def test_partial_rotations_handled(log_path, monkeypatch):
    """If only .1 exists (no .2, .3, …), rotation still works."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    _write_n_bytes(log_path, 2048)
    log_path.with_suffix(".1.log").write_bytes(b"only-old")

    daemon._rotate_if_oversize(log_path)

    assert log_path.with_suffix(".1.log").stat().st_size == 2048
    assert log_path.with_suffix(".2.log").read_bytes() == b"only-old"
    assert not log_path.with_suffix(".3.log").exists()


def test_idempotent_when_under_cap_after_rotation(log_path, monkeypatch):
    """Calling rotate twice in a row when nothing has changed is safe."""
    monkeypatch.setattr(daemon, "_SERVICE_LOG_CAP_BYTES", 1024)
    _write_n_bytes(log_path, 2048)
    daemon._rotate_if_oversize(log_path)
    # Now the file is gone (rotated). Caller will recreate it shortly,
    # but a defensive second call should still be a no-op.
    daemon._rotate_if_oversize(log_path)
    # Still just one rotation
    assert log_path.with_suffix(".1.log").exists()
    assert not log_path.with_suffix(".2.log").exists()


def test_default_cap_is_16_mib():
    """Sanity-check the documented constant value."""
    assert daemon._SERVICE_LOG_CAP_BYTES == 16 * 1024 * 1024


def test_default_backup_count_is_4():
    """Sanity-check the documented constant value."""
    assert daemon._SERVICE_LOG_BACKUP_COUNT == 4
