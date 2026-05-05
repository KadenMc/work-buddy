"""Tests for ``daemon._resolve_child_python``.

Children spawned by the sidecar inherit the daemon's interpreter
unless ``sidecar.python_executable`` is set. The pin matters most on
Windows scheduled tasks where ``conda activate`` can silently no-op,
leaving the daemon (and therefore every child) on the base interpreter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from work_buddy.sidecar import daemon


def test_resolve_returns_sys_executable_when_unset():
    """No config field set → fall back to the daemon's own interpreter."""
    assert daemon._resolve_child_python(cfg={}) == sys.executable
    assert daemon._resolve_child_python(cfg={"sidecar": {}}) == sys.executable


def test_resolve_uses_pinned_path_when_set_and_exists(tmp_path):
    """Config pin points at a real file → use it."""
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"")  # exists
    cfg = {"sidecar": {"python_executable": str(fake_python)}}
    assert daemon._resolve_child_python(cfg=cfg) == str(fake_python)


def test_resolve_falls_back_when_pinned_path_missing(tmp_path, caplog):
    """Pinned path doesn't exist → fall back to sys.executable AND log
    an error so the misconfiguration is visible. The previous silent
    inheritance of sys.executable was the failure mode that let a
    base-env scheduled task spawn base-env children for weeks."""
    bogus = tmp_path / "does-not-exist" / "python.exe"
    cfg = {"sidecar": {"python_executable": str(bogus)}}
    with caplog.at_level("ERROR"):
        result = daemon._resolve_child_python(cfg=cfg)
    assert result == sys.executable
    messages = [rec.getMessage() for rec in caplog.records]
    # The logger uses %r which escapes backslashes on Windows, so we
    # compare against a unique fragment of the path rather than the
    # whole stringified Path.
    assert any(
        "does not exist" in m and "does-not-exist" in m for m in messages
    ), f"must log an error naming the bogus path; got: {messages}"


def test_resolve_warns_when_pinned_differs_from_sys_executable(tmp_path, caplog):
    """Pinned path is valid but != sys.executable → log a warning. This
    is intentional in some setups, but unintentional cases (daemon
    booted on base, config pins work-buddy env) need the diagnostic."""
    fake_python = tmp_path / "different_python.exe"
    fake_python.write_bytes(b"")
    cfg = {"sidecar": {"python_executable": str(fake_python)}}
    with caplog.at_level("WARNING"):
        result = daemon._resolve_child_python(cfg=cfg)
    assert result == str(fake_python)
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "differs from sys.executable" in m for m in messages
    ), f"must surface the daemon-vs-children interpreter mismatch; got: {messages}"


def test_resolve_silent_when_pinned_matches_sys_executable():
    """Pin matches the daemon's interpreter → no warning, return
    sys.executable. Common case: user pinned and is running cleanly."""
    cfg = {"sidecar": {"python_executable": sys.executable}}
    # Should resolve without error.
    result = daemon._resolve_child_python(cfg=cfg)
    assert Path(result).resolve() == Path(sys.executable).resolve()
