"""Host-native filesystem selection for the local Co-work dashboard.

The browser cannot safely turn a client-side directory handle into a path on the
machine running Work Buddy. Local desktop installs therefore ask the host OS to
choose a Folder, Markdown file, or destination directory; hosts without a
graphical picker report it as unavailable.
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
HostScopedPathChooser = Callable[[str | Path], str | Path | None]

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
    cancelled_code: int | None = 1,
    empty_is_none: bool = True,
    selection_label: str = "Folder",
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
            f"The {selection_label} picker took too long to respond.",
            code="folder_chooser_timeout",
            status=504,
            diagnostic=_diagnostic_excerpt(f"TimeoutExpired: {exc}"),
        ) from exc
    except OSError as exc:
        raise NativeFolderChooserError(
            f"The {selection_label} picker could not be opened.",
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
            f"The {selection_label} picker closed unexpectedly.",
            diagnostic=_diagnostic_excerpt(diagnostic),
        )
    selected = completed.stdout
    # osascript and zenity terminate their one-value stdout protocol with a
    # newline. Remove that framing only; ``strip()`` would silently change a
    # legitimately selected POSIX path ending in spaces or tabs. A second line
    # is never part of this protocol and must fail closed.
    if selected.endswith("\n"):
        selected = selected[:-1]
        if selected.endswith("\r"):
            selected = selected[:-1]
    if "\r" in selected or "\n" in selected:
        raise NativeFolderChooserError(
            f"The {selection_label} picker returned an invalid result.",
            retryable=False,
            diagnostic="Native picker output contained multiple lines.",
        )
    if not selected and empty_is_none:
        return None
    return selected


def _macos_cancel_marker(mode: str) -> str:
    """Return the mode-bound success payload used for AppleScript cancellation."""

    from work_buddy.cowork.folder_picker_helper import PICKER_PROTOCOL

    return f"{PICKER_PROTOCOL}:cancel:{mode}"


def _parse_macos_result(raw: str | None, *, expected_mode: str) -> str | None:
    """Accept only the expected cancel marker or an absolute POSIX path."""

    from work_buddy.cowork.folder_picker_helper import PICKER_PROTOCOL

    expected_cancel = _macos_cancel_marker(expected_mode)
    if raw == expected_cancel:
        return None
    if (
        not isinstance(raw, str)
        or not raw
        or raw.startswith(f"{PICKER_PROTOCOL}:cancel:")
        or not raw.startswith("/")
        or "\x00" in raw
        or len(raw) > _MAX_SELECTED_PATH_CHARS
    ):
        raise NativeFolderChooserError(
            "The picker returned an invalid result.",
            retryable=False,
            diagnostic="macOS native picker emitted an invalid protocol payload.",
        )
    return raw


def _validated_start_directory(value: str | Path | None) -> Path:
    """Bound and validate dynamic picker arguments before spawning a helper."""

    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise NativeFolderChooserError(
            "The picker could not use this Folder.",
            retryable=False,
            diagnostic="Picker start directory was not path-like.",
        ) from exc
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or len(raw) > _MAX_SELECTED_PATH_CHARS
    ):
        raise NativeFolderChooserError(
            "The picker could not use this Folder.",
            retryable=False,
            diagnostic="Picker start directory failed bounded validation.",
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise NativeFolderChooserError(
            "The picker could not use this Folder.",
            retryable=False,
            diagnostic="Picker start directory was not absolute.",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeFolderChooserError(
            "The picker could not use this Folder.",
            retryable=False,
            diagnostic=_diagnostic_excerpt(
                f"Picker start directory was unavailable: {type(exc).__name__}"
            ),
        ) from exc
    if not resolved.is_dir():
        raise NativeFolderChooserError(
            "The picker could not use this Folder.",
            retryable=False,
            diagnostic="Picker start directory was not a directory.",
        )
    return resolved


def _parse_helper_result(raw: str, *, expected_mode: str) -> str:
    from work_buddy.cowork.folder_picker_helper import (
        PICKER_PROTOCOL,
    )

    if len(raw) > _MAX_HELPER_OUTPUT_CHARS:
        raise NativeFolderChooserError(
            "The picker returned an invalid result.",
            retryable=False,
            diagnostic="Native picker helper output exceeded the protocol limit.",
        )
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NativeFolderChooserError(
            "The picker returned an invalid result.",
            retryable=False,
            diagnostic="Native picker helper emitted invalid JSON.",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != PICKER_PROTOCOL
        or payload.get("mode") != expected_mode
        or not isinstance(payload.get("path"), str)
        or not payload["path"]
        or "\x00" in payload["path"]
        or len(payload["path"]) > _MAX_SELECTED_PATH_CHARS
    ):
        raise NativeFolderChooserError(
            "The picker returned an invalid result.",
            retryable=False,
            diagnostic="Native picker helper emitted an invalid protocol payload.",
        )
    return payload["path"]


def _choose_windows(
    python: str = sys.executable,
    *,
    mode: str | None = None,
    start_directory: str | Path | None = None,
) -> str | None:
    from work_buddy.cowork.folder_picker_helper import (
        PICKER_CANCELLED,
        PICKER_MODE_FOLDER,
        PICKER_MODE_LOCATION,
        PICKER_MODE_MARKDOWN,
    )

    requested_mode = mode or PICKER_MODE_FOLDER
    if requested_mode not in {
        PICKER_MODE_FOLDER,
        PICKER_MODE_MARKDOWN,
        PICKER_MODE_LOCATION,
    }:
        raise NativeFolderChooserError(
            "The picker could not be opened.",
            retryable=False,
            diagnostic="Unsupported native picker mode.",
        )
    command = [python, "-I", "-m", _WINDOWS_HELPER_MODULE]
    selection_label = "Folder"
    if requested_mode != PICKER_MODE_FOLDER:
        start = _validated_start_directory(start_directory)
        command.extend(
            ["--mode", requested_mode, "--start", str(start)]
        )
        if requested_mode == PICKER_MODE_MARKDOWN:
            selection_label = "Markdown file"
    raw = _run_dialog(
        command,
        cancelled_code=PICKER_CANCELLED,
        empty_is_none=False,
        selection_label=selection_label,
    )
    if raw is None:
        return None
    return _parse_helper_result(raw, expected_mode=requested_mode)



def _choose_macos(
    *,
    mode: str | None = None,
    start_directory: str | Path | None = None,
) -> str | None:
    from work_buddy.cowork.folder_picker_helper import (
        PICKER_MODE_FOLDER,
        PICKER_MODE_LOCATION,
        PICKER_MODE_MARKDOWN,
    )

    requested_mode = mode or PICKER_MODE_FOLDER
    cancel_marker = _macos_cancel_marker(requested_mode)
    if requested_mode == PICKER_MODE_FOLDER:
        script = (
            'try\nPOSIX path of (choose folder with prompt "Open Folder")\n'
            f'on error number -128\nreturn "{cancel_marker}"\nend try'
        )
        command = ["/usr/bin/osascript", "-e", script]
        selection_label = "Folder"
    else:
        start = _validated_start_directory(start_directory)
        if requested_mode == PICKER_MODE_MARKDOWN:
            picker = (
                'choose file with prompt "New from Markdown" '
                'default location (POSIX file (item 1 of argv)) '
                'of type {"md", "markdown"}'
            )
            selection_label = "Markdown file"
        elif requested_mode == PICKER_MODE_LOCATION:
            picker = (
                'choose folder with prompt "Choose Location" '
                'default location (POSIX file (item 1 of argv))'
            )
            selection_label = "Folder"
        else:
            raise NativeFolderChooserError(
                "The picker could not be opened.",
                retryable=False,
                diagnostic="Unsupported native picker mode.",
            )
        script = (
            "on run argv\ntry\n"
            f"POSIX path of ({picker})\n"
            f'on error number -128\nreturn "{cancel_marker}"\n'
            "end try\nend run"
        )
        command = ["/usr/bin/osascript", "-e", script, "--", str(start)]
    raw = _run_dialog(
        command,
        # AppleScript errors always make osascript exit nonzero; cancellation is
        # therefore returned as an explicit success payload instead. This keeps
        # every genuine script failure on the error path.
        cancelled_code=None,
        empty_is_none=False,
        selection_label=selection_label,
    )
    return _parse_macos_result(raw, expected_mode=requested_mode)


def _choose_zenity(
    zenity: str,
    *,
    mode: str | None = None,
    start_directory: str | Path | None = None,
) -> str | None:
    from work_buddy.cowork.folder_picker_helper import (
        PICKER_MODE_FOLDER,
        PICKER_MODE_LOCATION,
        PICKER_MODE_MARKDOWN,
    )

    requested_mode = mode or PICKER_MODE_FOLDER
    command = [zenity, "--file-selection"]
    selection_label = "Folder"
    if requested_mode == PICKER_MODE_FOLDER:
        command.extend(["--directory", "--title=Open Folder"])
    else:
        start = _validated_start_directory(start_directory)
        command.append(f"--filename={str(start) + os.sep}")
        if requested_mode == PICKER_MODE_MARKDOWN:
            command.extend(
                [
                    "--title=New from Markdown",
                    "--file-filter=Markdown files | *.md *.markdown",
                ]
            )
            selection_label = "Markdown file"
        elif requested_mode == PICKER_MODE_LOCATION:
            command.extend(["--directory", "--title=Choose Location"])
        else:
            raise NativeFolderChooserError(
                "The picker could not be opened.",
                retryable=False,
                diagnostic="Unsupported native picker mode.",
            )
    return _run_dialog(command, selection_label=selection_label)


def _locked_picker_call(implementation: Callable[[], str | None]) -> str | None:
    """Serialize all native dialogs, including different picker modes."""

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
        return _locked_picker_call(implementation)

    return choose


def _default_scoped_path_chooser(mode: str) -> HostScopedPathChooser | None:
    """Build a root-started picker for Markdown or destination selection."""

    implementation: Callable[[Path], str | None] | None = None
    if os.name == "nt":
        if importlib.util.find_spec("PySide6") is not None:
            implementation = lambda start: _choose_windows(
                mode=mode,
                start_directory=start,
            )
    elif sys.platform == "darwin" and Path("/usr/bin/osascript").is_file():
        implementation = lambda start: _choose_macos(
            mode=mode,
            start_directory=start,
        )
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        zenity = shutil.which("zenity")
        if zenity is not None:
            implementation = lambda start: _choose_zenity(
                zenity,
                mode=mode,
                start_directory=start,
            )

    if implementation is None:
        return None

    def choose(start_directory: str | Path) -> str | None:
        start = _validated_start_directory(start_directory)
        return _locked_picker_call(lambda: implementation(start))

    return choose


def default_host_markdown_chooser() -> HostScopedPathChooser | None:
    """Return the native Markdown-file chooser supported by this host."""

    from work_buddy.cowork.folder_picker_helper import PICKER_MODE_MARKDOWN

    return _default_scoped_path_chooser(PICKER_MODE_MARKDOWN)


def default_host_location_chooser() -> HostScopedPathChooser | None:
    """Return the native destination-Folder chooser supported by this host."""

    from work_buddy.cowork.folder_picker_helper import PICKER_MODE_LOCATION

    return _default_scoped_path_chooser(PICKER_MODE_LOCATION)


__all__ = [
    "HostFolderChooser",
    "HostScopedPathChooser",
    "NativeFolderChooserError",
    "default_host_folder_chooser",
    "default_host_location_chooser",
    "default_host_markdown_chooser",
]
