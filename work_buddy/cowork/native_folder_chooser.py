"""Host-native directory selection for the local Co-work dashboard.

The browser cannot safely turn a client-side directory handle into a path on the
machine running Work Buddy.  Local installs therefore ask the host OS to choose a
directory; remote/headless installs keep the explicit host-path fallback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path


HostFolderChooser = Callable[[], str | Path | None]

_DIALOG_LOCK = threading.Lock()
_WINDOWS_CANCELLED = 2


class NativeFolderChooserError(RuntimeError):
    """The host advertised a native picker but could not open it."""


def _run_dialog(command: list[str], *, cancelled_code: int = 1) -> str | None:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 600,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(command, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeFolderChooserError("The host Folder chooser could not be opened.") from exc
    if completed.returncode == cancelled_code:
        return None
    if completed.returncode != 0:
        raise NativeFolderChooserError("The host Folder chooser closed unexpectedly.")
    selected = completed.stdout.strip()
    return selected or None


def _choose_windows(powershell: str) -> str | None:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$picker = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$picker.Description = 'Choose a Folder for Co-work'; "
        "$picker.ShowNewFolderButton = $true; "
        "$result = $picker.ShowDialog(); "
        "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::Write($picker.SelectedPath); exit 0 }; exit 2"
    )
    return _run_dialog(
        [powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
        cancelled_code=_WINDOWS_CANCELLED,
    )


def _choose_macos() -> str | None:
    script = (
        'try\nPOSIX path of (choose folder with prompt "Choose a Folder for Co-work")\n'
        'on error number -128\nerror number 2\nend try'
    )
    return _run_dialog(["/usr/bin/osascript", "-e", script], cancelled_code=2)


def _choose_zenity(zenity: str) -> str | None:
    return _run_dialog(
        [
            zenity,
            "--file-selection",
            "--directory",
            "--title=Choose a Folder for Co-work",
        ]
    )


def default_host_folder_chooser() -> HostFolderChooser | None:
    """Return the supported chooser for this host, or ``None`` when headless.

    Detection is intentionally conservative.  Reporting the chooser as unavailable
    gives remote dashboards a truthful host-path field instead of a button that can
    never surface a dialog.
    """

    implementation: Callable[[], str | None] | None = None
    if os.name == "nt":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is not None:
            implementation = lambda: _choose_windows(powershell)
    elif sys.platform == "darwin" and Path("/usr/bin/osascript").is_file():
        implementation = _choose_macos
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        zenity = shutil.which("zenity")
        if zenity is not None:
            implementation = lambda: _choose_zenity(zenity)

    if implementation is None:
        return None

    def choose() -> str | None:
        if not _DIALOG_LOCK.acquire(blocking=False):
            raise NativeFolderChooserError("A Folder chooser is already open.")
        try:
            return implementation()
        finally:
            _DIALOG_LOCK.release()

    return choose


__all__ = [
    "HostFolderChooser",
    "NativeFolderChooserError",
    "default_host_folder_chooser",
]
