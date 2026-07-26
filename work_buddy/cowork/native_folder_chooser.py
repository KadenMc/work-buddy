"""Host-native directory selection for the local Co-work dashboard.

The browser cannot safely turn a client-side directory handle into a path on the
machine running Work Buddy. Local desktop installs therefore ask the host OS to
choose a directory; hosts without a graphical picker report it as unavailable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path


HostFolderChooser = Callable[[], str | Path | None]

_DIALOG_LOCK = threading.Lock()
_DIALOG_TIMEOUT_SECONDS = 120
_MAX_HELPER_OUTPUT_CHARS = 65_536
_MAX_SELECTED_PATH_CHARS = 32_767
_WINDOWS_HELPER_MODULE = "work_buddy.cowork.folder_picker_helper"


class NativeFolderChooserError(RuntimeError):
    """The host advertised a native picker but could not open it."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "folder_chooser_failed",
        status: int = 503,
        retryable: bool = True,
        diagnostic: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.diagnostic = diagnostic or message


def _diagnostic_excerpt(value: object, *, limit: int = 1000) -> str:
    """Keep child-process diagnostics single-line and bounded for local logs."""

    return " ".join(str(value).split())[:limit]


def _run_dialog(
    command: list[str],
    *,
    cancelled_code: int = 1,
    empty_is_none: bool = True,
) -> str | None:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": _DIALOG_TIMEOUT_SECONDS,
        "check": False,
        "shell": False,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise NativeFolderChooserError(
            "The Folder picker took too long to respond.",
            code="folder_chooser_timeout",
            status=504,
            diagnostic=_diagnostic_excerpt(f"TimeoutExpired: {exc}"),
        ) from exc
    except OSError as exc:
        raise NativeFolderChooserError(
            "The Folder picker could not be opened.",
            diagnostic=_diagnostic_excerpt(f"{type(exc).__name__}: {exc}"),
        ) from exc
    if completed.returncode == cancelled_code:
        return None
    if completed.returncode != 0:
        stderr = _diagnostic_excerpt(completed.stderr)
        diagnostic = f"picker exited with status {completed.returncode}"
        if stderr:
            diagnostic += f": {stderr}"
        raise NativeFolderChooserError(
            "The Folder picker closed unexpectedly.",
            diagnostic=_diagnostic_excerpt(diagnostic),
        )
    selected = completed.stdout.strip()
    if not selected and empty_is_none:
        return None
    return selected


def _choose_windows(python: str = sys.executable) -> str | None:
    from work_buddy.cowork.folder_picker_helper import (
        PICKER_CANCELLED,
        PICKER_PROTOCOL,
    )

    raw = _run_dialog(
        [python, "-I", "-m", _WINDOWS_HELPER_MODULE],
        cancelled_code=PICKER_CANCELLED,
        empty_is_none=False,
    )
    if raw is None:
        return None
    if len(raw) > _MAX_HELPER_OUTPUT_CHARS:
        raise NativeFolderChooserError(
            "The Folder picker returned an invalid result.",
            retryable=False,
            diagnostic="Folder picker helper output exceeded the protocol limit.",
        )
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NativeFolderChooserError(
            "The Folder picker returned an invalid result.",
            retryable=False,
            diagnostic="Folder picker helper emitted invalid JSON.",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != PICKER_PROTOCOL
        or not isinstance(payload.get("path"), str)
        or not payload["path"]
        or "\x00" in payload["path"]
        or len(payload["path"]) > _MAX_SELECTED_PATH_CHARS
    ):
        raise NativeFolderChooserError(
            "The Folder picker returned an invalid result.",
            retryable=False,
            diagnostic="Folder picker helper emitted an invalid protocol payload.",
        )
    return payload["path"]


def _choose_macos() -> str | None:
    script = (
        'try\nPOSIX path of (choose folder with prompt "Open Folder")\n'
        'on error number -128\nerror number 2\nend try'
    )
    return _run_dialog(["/usr/bin/osascript", "-e", script], cancelled_code=2)


def _choose_zenity(zenity: str) -> str | None:
    return _run_dialog(
        [
            zenity,
            "--file-selection",
            "--directory",
            "--title=Open Folder",
        ]
    )


def default_host_folder_chooser() -> HostFolderChooser | None:
    """Return the supported chooser for this host, or ``None`` when headless.

    Detection is intentionally conservative so unavailable hosts do not advertise
    a button that can never surface a dialog.
    """

    implementation: Callable[[], str | None] | None = None
    if os.name == "nt":
        if importlib.util.find_spec("PySide6") is not None:
            implementation = _choose_windows
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
            raise NativeFolderChooserError(
                "A Folder picker is already open.",
                code="folder_chooser_busy",
                status=409,
            )
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
