"""Tests for the child-spawn env injection shared by work-buddy's launchers.

``compat.build_child_env()`` must emit the UTF-8 and native numerical runtime
defaults every supervised child needs before its Python imports begin.
"""

import os

from work_buddy import compat


def test_build_child_env_sets_pythonutf8(monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    env = compat.build_child_env()
    assert env["PYTHONUTF8"] == "1"


def test_build_child_env_preserves_user_override(monkeypatch):
    """If the user explicitly set PYTHONUTF8=0 (e.g. debugging a bytes-vs-str
    regression), don't clobber it."""
    monkeypatch.setenv("PYTHONUTF8", "0")
    env = compat.build_child_env()
    assert env["PYTHONUTF8"] == "0"


def test_build_child_env_caps_openblas_by_default(monkeypatch):
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    env = compat.build_child_env()
    assert env["OPENBLAS_NUM_THREADS"] == "1"


def test_build_child_env_preserves_openblas_override(monkeypatch):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")
    env = compat.build_child_env()
    assert env["OPENBLAS_NUM_THREADS"] == "4"


def test_build_child_env_does_not_mutate_os_environ(monkeypatch):
    """Returning a copy is load-bearing: mutating os.environ would leak
    PYTHONUTF8 into the parent and any subprocess that bypasses this helper."""
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    snapshot = dict(os.environ)
    _ = compat.build_child_env()
    assert dict(os.environ) == snapshot


def test_build_child_env_inherits_parent_env(monkeypatch):
    monkeypatch.setenv("WB_TEST_MARKER", "present")
    env = compat.build_child_env()
    assert env.get("WB_TEST_MARKER") == "present"
