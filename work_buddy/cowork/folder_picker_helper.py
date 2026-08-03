"""Isolated Qt host process for Co-work's native filesystem pickers.

The dashboard service is not a GUI process.  This module gives the native
dialog its own main thread and ``QApplication`` without asking a command shell
to compile or evaluate code at runtime. Its stdout is a deliberately tiny
machine protocol consumed only by :mod:`work_buddy.cowork.native_folder_chooser`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from work_buddy.cowork.file_importers import (
    DEFAULT_FILE_IMPORTERS,
    FilePickerSpec,
    MARKDOWN_SUFFIXES,
)


PICKER_PROTOCOL = "work-buddy-native-picker/v2"
PICKER_CANCELLED = 2
PICKER_MODE_FOLDER = "folder"
PICKER_MODE_FILE = "file"
PICKER_MODE_MARKDOWN = "markdown"
PICKER_MODE_LOCATION = "location"
PICKER_MODES = frozenset(
    {
        PICKER_MODE_FILE,
        PICKER_MODE_FOLDER,
        PICKER_MODE_MARKDOWN,
        PICKER_MODE_LOCATION,
    }
)
MAX_START_PATH_CHARS = 32_767


def _qt_file_filter(spec: FilePickerSpec) -> str:
    return f"{spec.display_name} ({' '.join(spec.patterns)})"


SUPPORTED_FILE_PICKER_SPEC = DEFAULT_FILE_IMPORTERS.picker_spec()
SUPPORTED_FILE_FILTER = _qt_file_filter(SUPPORTED_FILE_PICKER_SPEC)
# Compatibility mode stays Markdown-only even after generic From file gains
# another registered importer.
MARKDOWN_FILE_PICKER_SPEC = FilePickerSpec(
    display_name="Markdown files",
    suffixes=MARKDOWN_SUFFIXES,
)
MARKDOWN_FILE_FILTER = _qt_file_filter(MARKDOWN_FILE_PICKER_SPEC)


def _validate_start_directory(value: str | None) -> Path:
    """Validate the sole dynamic helper argument before opening host UI."""

    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value) > MAX_START_PATH_CHARS
    ):
        raise ValueError("picker start directory is invalid")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("picker start directory must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("picker start directory is unavailable") from exc
    if not resolved.is_dir():
        raise ValueError("picker start directory is not a directory")
    return resolved


def _choose_native_path(
    mode: str,
    start_directory: Path,
) -> str | None:
    """Open one platform-native picker through Qt."""

    if mode not in PICKER_MODES:
        raise ValueError("unsupported picker mode")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication([])
    app.setApplicationName("Work Buddy")
    app.setOrganizationName("Work Buddy")
    anchor = QWidget()
    anchor.setWindowFlag(Qt.WindowType.Tool, True)
    anchor.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    anchor.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    # A fully transparent window is not activatable on every Windows build.
    # One percent keeps this one-pixel owner effectively invisible while still
    # allowing the native dialog to inherit its modal/topmost relationship.
    anchor.setWindowOpacity(0.01)
    anchor.resize(1, 1)
    screen = app.primaryScreen()
    if screen is not None:
        anchor.move(screen.availableGeometry().center())
    anchor.show()
    anchor.raise_()
    anchor.activateWindow()
    app.processEvents()
    try:
        if mode in {PICKER_MODE_FILE, PICKER_MODE_MARKDOWN}:
            file_filter = (
                SUPPORTED_FILE_FILTER
                if mode == PICKER_MODE_FILE
                else MARKDOWN_FILE_FILTER
            )
            selected, _selected_filter = QFileDialog.getOpenFileName(
                anchor,
                "From file",
                str(start_directory),
                file_filter,
            )
        else:
            selected = QFileDialog.getExistingDirectory(
                anchor,
                "Open folder" if mode == PICKER_MODE_FOLDER else "Choose Location",
                str(start_directory),
                QFileDialog.Option.ShowDirsOnly,
            )
        return str(selected) if selected else None
    finally:
        anchor.close()
        if owns_app:
            app.quit()


def _choose_native_folder() -> str | None:
    """Open the existing folder chooser with its original home start."""

    return _choose_native_path(PICKER_MODE_FOLDER, Path.home())


def _choose_native_markdown(start_directory: str | Path) -> str | None:
    """Compatibility wrapper for the former Markdown-specific picker."""

    return _choose_native_path(
        PICKER_MODE_MARKDOWN,
        _validate_start_directory(str(start_directory)),
    )


def _choose_native_file(start_directory: str | Path) -> str | None:
    """Choose one supported import file, starting inside the active folder."""

    return _choose_native_path(
        PICKER_MODE_FILE,
        _validate_start_directory(str(start_directory)),
    )


def _choose_native_location(start_directory: str | Path) -> str | None:
    """Choose a destination directory, starting inside the active folder."""

    return _choose_native_path(
        PICKER_MODE_LOCATION,
        _validate_start_directory(str(start_directory)),
    )


def run_picker(
    chooser: Callable[[], str | None] = _choose_native_folder,
    *,
    mode: str = PICKER_MODE_FOLDER,
) -> int:
    """Run ``chooser`` and emit the stable parent-process protocol."""

    if mode not in PICKER_MODES:
        print("Native picker mode is invalid.", file=sys.stderr)
        return 1
    try:
        selected = chooser()
    except Exception as exc:
        print(
            f"Native picker failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    if selected is None:
        return PICKER_CANCELLED
    if not isinstance(selected, str) or not selected:
        print("Native picker returned an invalid selection.", file=sys.stderr)
        return 1
    json.dump(
        {"protocol": PICKER_PROTOCOL, "mode": mode, "path": selected},
        sys.stdout,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return 0


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="work-buddy-native-picker",
        description="Work Buddy native picker helper",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(PICKER_MODES),
        default=PICKER_MODE_FOLDER,
    )
    parser.add_argument("--start")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _arguments(argv)
        mode = str(arguments.mode)
        if mode == PICKER_MODE_FOLDER:
            if arguments.start is not None:
                raise ValueError("the folder picker does not accept a start argument")
            chooser = _choose_native_folder
        else:
            start = _validate_start_directory(arguments.start)
            if mode == PICKER_MODE_FILE:
                chooser = lambda: _choose_native_file(start)
            elif mode == PICKER_MODE_MARKDOWN:
                chooser = lambda: _choose_native_markdown(start)
            else:
                chooser = lambda: _choose_native_location(start)
    except (SystemExit, ValueError) as exc:
        if isinstance(exc, SystemExit):
            # ``argparse`` uses exit status 2 for malformed CLI arguments, but
            # this helper reserves 2 exclusively for a user cancelling the
            # native dialog. Normalize parser failures so the parent process
            # can never misreport version skew or a bad invocation as cancel.
            return 0 if not exc.code else 1
        print(f"Native picker request is invalid: {exc}", file=sys.stderr)
        return 1
    return run_picker(chooser, mode=mode)


if __name__ == "__main__":
    raise SystemExit(main())
