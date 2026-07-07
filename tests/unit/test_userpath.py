"""Tests for the wbuddy PATH shim (``work_buddy.userpath``).

The pure PATH-string surgery is tested directly; everything that would touch
the host (registry writes, ``~/.local/bin``) is monkeypatched to temp paths or
stubs, so the tests never modify the machine they run on.
"""

from __future__ import annotations

import os

import pytest

from work_buddy import userpath


# --- pure PATH-string surgery ----------------------------------------------

def _sep(*segments: str) -> str:
    return os.pathsep.join(segments)


def test_merge_appends_when_absent():
    assert userpath.merge_path(_sep("a", "b"), "c") == _sep("a", "b", "c")


def test_merge_into_empty_path():
    assert userpath.merge_path("", "c") == "c"


def test_merge_returns_none_when_present():
    assert userpath.merge_path(_sep("a", "c", "b"), "c") is None


def test_merge_is_trailing_separator_insensitive(monkeypatch):
    # Segments join with os.pathsep so the Windows normalization semantics are
    # exercised on any host OS (CI runs Linux, where the separator is ':').
    monkeypatch.setattr(userpath, "IS_WINDOWS", True)
    current = _sep(r"C:\x", "C:\\tool\\bin\\", r"C:\y")
    assert userpath.merge_path(current, r"C:\tool\bin") is None


def test_merge_is_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr(userpath, "IS_WINDOWS", True)
    assert userpath.merge_path(_sep(r"C:\Tool\Bin"), r"c:\tool\bin") is None


def test_merge_is_case_sensitive_on_posix(monkeypatch):
    monkeypatch.setattr(userpath, "IS_WINDOWS", False)
    assert userpath.merge_path("/Tool/bin", "/tool/bin") is not None


def test_merge_preserves_existing_value_verbatim(monkeypatch):
    # %VAR% references in a REG_EXPAND_SZ value must survive untouched.
    monkeypatch.setattr(userpath, "IS_WINDOWS", True)
    current = _sep(r"%SystemRoot%\system32", r"%USERPROFILE%\.local\bin")
    merged = userpath.merge_path(current, r"C:\wb\bin")
    assert merged == current + os.pathsep + r"C:\wb\bin"


def test_strip_removes_entry():
    assert userpath.strip_path(_sep("a", "c", "b"), "c") == _sep("a", "b")


def test_strip_returns_none_when_absent():
    assert userpath.strip_path(_sep("a", "b"), "c") is None


def test_strip_drops_empty_segments():
    assert userpath.strip_path(_sep("a", "", "c"), "c") == "a"


def test_merge_then_strip_roundtrips():
    merged = userpath.merge_path(_sep("a", "b"), "c")
    assert userpath.strip_path(merged, "c") == _sep("a", "b")


# --- shim install / uninstall (host fully mocked) ---------------------------

@pytest.fixture
def win_home(tmp_path, monkeypatch):
    monkeypatch.setattr(userpath, "IS_WINDOWS", True)
    calls = {}

    def fake_add(d):
        calls["added"] = d
        return {"ok": True, "changed": True, "detail": "added"}

    def fake_remove(d):
        calls["removed"] = d
        return {"ok": True, "changed": True, "detail": "removed"}

    monkeypatch.setattr(userpath, "add_dir_to_user_path", fake_add)
    monkeypatch.setattr(userpath, "remove_dir_from_user_path", fake_remove)
    home = tmp_path / "wb-home"
    (home / ".venv" / "Scripts").mkdir(parents=True)
    (home / ".venv" / "Scripts" / "wbuddy.exe").write_bytes(b"MZ")
    return home, calls


@pytest.fixture
def posix_home(tmp_path, monkeypatch):
    monkeypatch.setattr(userpath, "IS_WINDOWS", False)
    monkeypatch.setattr(userpath, "_posix_bin_dir", lambda: tmp_path / "local-bin")
    home = tmp_path / "wb-home"
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / ".venv" / "bin" / "wbuddy").write_text("#!fake")
    return home, tmp_path / "local-bin"


def test_windows_install_writes_shim_and_updates_path(win_home):
    home, calls = win_home
    res = userpath.install_cli_shim(home)
    assert res["ok"] is True
    shim = home / "bin" / "wbuddy.cmd"
    assert shim.exists()
    # Relative to the shim's own location, so the pair survives as a unit.
    assert "%~dp0" in shim.read_text(encoding="utf-8")
    assert "wbuddy.exe" in shim.read_text(encoding="utf-8")
    assert calls["added"] == str(home / "bin")


def test_windows_uninstall_removes_shim_and_path_entry(win_home):
    home, calls = win_home
    userpath.install_cli_shim(home)
    res = userpath.uninstall_cli_shim(home)
    assert res["ok"] is True
    assert not (home / "bin" / "wbuddy.cmd").exists()
    assert not (home / "bin").exists()  # empty dir cleaned up
    assert calls["removed"] == str(home / "bin")


def test_skips_when_venv_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(userpath, "IS_WINDOWS", True)
    res = userpath.install_cli_shim(tmp_path)  # no .venv at all
    assert res["ok"] is True and res["changed"] is False
    assert not (tmp_path / "bin").exists()


def test_posix_install_writes_exec_shim(posix_home):
    home, bin_dir = posix_home
    res = userpath.install_cli_shim(home)
    assert res["ok"] is True
    shim = bin_dir / "wbuddy"
    text = shim.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert str(home.resolve()) in text


def test_posix_uninstall_only_removes_own_shim(posix_home, tmp_path):
    home, bin_dir = posix_home
    userpath.install_cli_shim(home)
    # A shim pointing at a DIFFERENT install must be left alone.
    other = tmp_path / "other-home"
    res = userpath.uninstall_cli_shim(other)
    assert (bin_dir / "wbuddy").exists()
    assert "different install" in res["detail"]
    # The owning install's uninstall removes it.
    userpath.uninstall_cli_shim(home)
    assert not (bin_dir / "wbuddy").exists()
