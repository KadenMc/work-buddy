"""Reading the sidecar state file while the daemon rewrites it.

The daemon replaces the state file atomically several times a minute, while
the tray reads it every few seconds. On Windows, a read that lands during the
replacement fails with ``PermissionError``. A reader that reported that as
"no state" made a healthy daemon look wedged for one poll: status showed
"wedged", and ``wbuddy start`` refused to run. Reads now ride out the
replacement.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from work_buddy.sidecar import state as sidecar_state
from work_buddy.sidecar.state import SidecarState


class _FlakyPath(type(Path())):
    """A state-file path whose first reads fail the way a concurrent
    replacement makes them fail."""

    failures: list[BaseException] = []

    def read_text(self, *args, **kwargs):  # type: ignore[override]
        if _FlakyPath.failures:
            raise _FlakyPath.failures.pop(0)
        return super().read_text(*args, **kwargs)


@pytest.fixture
def flaky_state_file(monkeypatch):
    path = _FlakyPath(sidecar_state.STATE_FILE)
    path.write_text(json.dumps({"pid": 4242, "started_at": 1.0, "last_tick_at": 2.0}))
    monkeypatch.setattr(sidecar_state, "STATE_FILE", path)
    monkeypatch.setattr(sidecar_state, "_READ_RETRY_DELAYS_S", (0.0, 0.0, 0.0, 0.0))
    _FlakyPath.failures = []
    yield _FlakyPath.failures
    _FlakyPath.failures = []


def test_a_read_during_a_replacement_is_retried(flaky_state_file, caplog) -> None:
    flaky_state_file.extend([PermissionError(13, "Permission denied")] * 2)

    with caplog.at_level(logging.WARNING, logger="work_buddy.sidecar.state"):
        state = sidecar_state.load_state()

    assert state is not None and state.pid == 4242
    assert "Failed to load sidecar state" not in caplog.text


def test_a_partial_file_is_retried(flaky_state_file) -> None:
    flaky_state_file.append(json.JSONDecodeError("Expecting value", "", 0))

    state = sidecar_state.load_state()

    assert state is not None and state.pid == 4242


def test_a_read_that_keeps_failing_is_reported(flaky_state_file, caplog) -> None:
    flaky_state_file.extend([PermissionError(13, "Permission denied")] * 5)

    with caplog.at_level(logging.WARNING, logger="work_buddy.sidecar.state"):
        state = sidecar_state.load_state()

    assert state is None
    assert "Failed to load sidecar state" in caplog.text


def test_a_file_removed_mid_read_is_simply_absent(flaky_state_file, caplog) -> None:
    flaky_state_file.append(FileNotFoundError(2, "No such file"))

    with caplog.at_level(logging.WARNING, logger="work_buddy.sidecar.state"):
        state = sidecar_state.load_state()

    assert state is None
    assert caplog.text == ""


def test_a_reader_never_misses_the_state_while_the_daemon_rewrites_it() -> None:
    """The real race: one thread rewrites the file the way the daemon does,
    another reads it the way the tray does. Every read must find the state."""
    sidecar_state.save_state(SidecarState(pid=4242, started_at=1.0))
    stop = threading.Event()
    write_errors: list[BaseException] = []

    def writer() -> None:
        tick = 0
        while not stop.is_set():
            tick += 1
            try:
                sidecar_state.save_state(
                    SidecarState(pid=4242, started_at=1.0, last_tick_at=float(tick)),
                )
            except BaseException as exc:  # surfaced below
                write_errors.append(exc)
                return

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    misses = 0
    reads = 0
    deadline = time.monotonic() + 2.0
    try:
        while time.monotonic() < deadline:
            reads += 1
            if sidecar_state.load_state() is None:
                misses += 1
    finally:
        stop.set()
        thread.join(timeout=30)

    assert write_errors == []
    assert reads > 50
    assert misses == 0
