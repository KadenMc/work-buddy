"""Isolated Qt host process for the native Co-work Folder picker.

The dashboard service is not a GUI process.  This module gives the native
dialog its own main thread and ``QApplication`` without asking a command shell
to compile or evaluate code at runtime.  Its stdout is a deliberately tiny
machine protocol consumed only by :mod:`work_buddy.cowork.native_folder_chooser`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path


PICKER_PROTOCOL = "work-buddy-folder-picker/v1"
PICKER_CANCELLED = 2


def _choose_native_folder() -> str | None:
    """Open the platform-native directory chooser through Qt."""

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
        selected = QFileDialog.getExistingDirectory(
            anchor,
            "Open Folder",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        return str(selected) if selected else None
    finally:
        anchor.close()
        if owns_app:
            app.quit()


def run_picker(
    chooser: Callable[[], str | None] = _choose_native_folder,
) -> int:
    """Run ``chooser`` and emit the stable parent-process protocol."""

    try:
        selected = chooser()
    except Exception as exc:
        print(
            f"Native Folder picker failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    if selected is None:
        return PICKER_CANCELLED
    if not isinstance(selected, str) or not selected:
        print("Native Folder picker returned an invalid selection.", file=sys.stderr)
        return 1
    json.dump(
        {"protocol": PICKER_PROTOCOL, "path": selected},
        sys.stdout,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return 0


def main() -> int:
    return run_picker()


if __name__ == "__main__":
    raise SystemExit(main())
