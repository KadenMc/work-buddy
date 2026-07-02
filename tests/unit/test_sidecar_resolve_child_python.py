"""Tests for ``compat.resolve_child_python``.

Children spawned by work-buddy inherit the parent's interpreter unless
``sidecar.python_executable`` is set. The pin matters most when the parent is
launched on the wrong interpreter (e.g. a login task started on the base env),
which would otherwise leave the parent and every child on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from work_buddy import compat


def test_resolve_returns_sys_executable_when_unset():
    """No config field set: fall back to the current interpreter."""
    assert compat.resolve_child_python(cfg={}) == sys.executable
    assert compat.resolve_child_python(cfg={"sidecar": {}}) == sys.executable


def test_resolve_uses_pinned_path_when_set_and_exists(tmp_path):
    """Config pin points at a real file: use it."""
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"")  # exists
    cfg = {"sidecar": {"python_executable": str(fake_python)}}
    assert compat.resolve_child_python(cfg=cfg) == str(fake_python)


def test_resolve_falls_back_when_pinned_path_missing(tmp_path, caplog):
    """Pinned path doesn't exist: fall back to sys.executable AND log an error
    so the misconfiguration is visible. Silent inheritance of sys.executable was
    the failure mode that let a base-env launch spawn base-env children."""
    bogus = tmp_path / "does-not-exist" / "python.exe"
    cfg = {"sidecar": {"python_executable": str(bogus)}}
    with caplog.at_level("ERROR"):
        result = compat.resolve_child_python(cfg=cfg)
    assert result == sys.executable
    messages = [rec.getMessage() for rec in caplog.records]
    # The logger uses %r which escapes backslashes on Windows, so compare
    # against a unique fragment of the path rather than the whole Path.
    assert any(
        "does not exist" in m and "does-not-exist" in m for m in messages
    ), f"must log an error naming the bogus path; got: {messages}"


def test_resolve_warns_when_pinned_differs_from_sys_executable(tmp_path, caplog):
    """Pinned path is valid but != sys.executable: log a warning. Intentional in
    some setups, but unintentional cases (parent on base, config pins the
    work-buddy env) need the diagnostic."""
    fake_python = tmp_path / "different_python.exe"
    fake_python.write_bytes(b"")
    cfg = {"sidecar": {"python_executable": str(fake_python)}}
    with caplog.at_level("WARNING"):
        result = compat.resolve_child_python(cfg=cfg)
    assert result == str(fake_python)
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "differs from sys.executable" in m for m in messages
    ), f"must surface the parent-vs-children interpreter mismatch; got: {messages}"


def test_resolve_silent_when_pinned_matches_sys_executable():
    """Pin matches the current interpreter: no warning, return sys.executable."""
    cfg = {"sidecar": {"python_executable": sys.executable}}
    result = compat.resolve_child_python(cfg=cfg)
    assert Path(result).resolve() == Path(sys.executable).resolve()
