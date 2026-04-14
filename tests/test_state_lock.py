"""Test save_state retry and fallback under Windows file locks.

Simulates the PermissionError crash from os.replace when another process
holds an exclusive handle on sidecar_state.json.
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from work_buddy.sidecar.state import SidecarState, save_state
import work_buddy.sidecar.state as state_mod

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows file-locking test"
)


@pytest.fixture()
def isolated_state_file(tmp_path):
    """Patch STATE_FILE to a temp location for the test."""
    state_file = tmp_path / "sidecar_state.json"
    state_file.write_text("{}")
    original = state_mod.STATE_FILE
    state_mod.STATE_FILE = state_file
    yield state_file
    state_mod.STATE_FILE = original


def _open_exclusive(path: Path):
    """Open a file with no sharing flags via Win32 API."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    assert handle != -1, f"CreateFileW failed: {ctypes.get_last_error()}"
    return handle, kernel32


def test_normal_save(isolated_state_file):
    """Baseline: save works when nothing holds the file."""
    s = SidecarState(started_at=1.0, pid=99)
    save_state(s)
    data = json.loads(isolated_state_file.read_text())
    assert data["pid"] == 99


def test_retry_recovers_after_lock_released(isolated_state_file):
    """Lock is held briefly, released mid-retry — save should succeed."""
    handle, kernel32 = _open_exclusive(isolated_state_file)

    def release():
        time.sleep(0.3)
        kernel32.CloseHandle(handle)

    threading.Thread(target=release, daemon=True).start()

    t0 = time.time()
    s = SidecarState(started_at=2.0, pid=100)
    save_state(s, _retries=4)
    elapsed = time.time() - t0

    data = json.loads(isolated_state_file.read_text())
    assert data["pid"] == 100
    assert elapsed >= 0.1, "Should have needed at least one retry"


def test_raises_when_permanently_locked(isolated_state_file):
    """Lock held through all retries and fallback — should raise."""
    handle, kernel32 = _open_exclusive(isolated_state_file)
    try:
        s = SidecarState(started_at=3.0, pid=101)
        # Use 1 retry to keep the test fast
        with pytest.raises(PermissionError):
            save_state(s, _retries=1)
    finally:
        kernel32.CloseHandle(handle)
