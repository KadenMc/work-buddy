"""Regression: a detached background launch must NOT use DETACHED_PROCESS.

On Windows, ``CREATE_NO_WINDOW | DETACHED_PROCESS`` is contradictory and
DETACHED_PROCESS wins: the process ends up with NO console at all. A
console-less process that then spawns console subprocesses (the sidecar runs
git-backed jobs on a schedule) gets a fresh *visible* console allocated per
child, which flashes a terminal window each time. ``CREATE_NO_WINDOW`` alone
gives the process a hidden console that its children inherit, so they stay
windowless, and it still outlives the launching shell because it owns that
console rather than borrowing the shell's.

Objective check: a helper launched with ``CREATE_NO_WINDOW | DETACHED_PROCESS``
reports ``GetConsoleCP() == 0`` (no console); with ``CREATE_NO_WINDOW`` alone it
reports a nonzero code page (a hidden console its children inherit).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from work_buddy import compat


@pytest.mark.skipif(sys.platform != "win32", reason="creationflags are Windows-only")
def test_detached_launch_uses_hidden_console_not_detached():
    flags = compat.detached_process_kwargs()["creationflags"]
    # A hidden console: children inherit it and stay windowless.
    assert flags & subprocess.CREATE_NO_WINDOW
    # NOT a console-less process: that pops a fresh window per console child.
    assert not (flags & subprocess.DETACHED_PROCESS)


def test_detached_kwargs_shape_per_platform():
    kw = compat.detached_process_kwargs()
    if sys.platform == "win32":
        assert set(kw) == {"creationflags"}
    else:
        assert kw == {"start_new_session": True}
