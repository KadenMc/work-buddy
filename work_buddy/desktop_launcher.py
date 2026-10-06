"""Console-less desktop launcher for the local work-buddy React application.

Windows installer shortcuts invoke this module through ``pythonw.exe``.  It
shares the exact lifecycle operation behind ``wbuddy launch`` but replaces
terminal diagnostics with a durable log and a native error dialog.
"""

from __future__ import annotations

import os
import shutil
import sys
import traceback
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

DESKTOP_SESSION_ID = "desktop-launcher"


def launcher_log_path() -> Path:
    """Return the durable user-data log named by launcher failure dialogs."""
    from work_buddy.paths import resolve

    return resolve("logs/desktop-launcher")


def _show_native_error(detail: str, log_path: Path) -> None:
    """Show a best-effort native failure surface for a console-less launcher."""
    message = f"{detail}\n\nSee the launcher log for details:\n{log_path}"
    if os.name == "nt":
        import ctypes

        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(0, message, "work-buddy could not open", 0x10010)
    elif sys.platform == "darwin":
        from work_buddy.process import run_tool

        run_tool(
            [
                "/usr/bin/osascript",
                "-e", "on run argv",
                "-e", 'display alert "Work Buddy could not open" message (item 1 of argv) as critical',
                "-e", "end run",
                message,
            ],
            timeout=15,
            text=False,
        )
    elif zenity := shutil.which("zenity"):
        from work_buddy.process import run_tool

        run_tool(
            [zenity, "--error", "--title=Work Buddy could not open", f"--text={message}"],
            timeout=15,
            text=False,
        )


def _write_event(stream, level: str, detail: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stream.write(f"{timestamp} | {level} | {detail}\n")
    stream.flush()


def main() -> int:
    """Launch work-buddy, reporting any failure without opening a terminal."""
    log_path = launcher_log_path()
    with ExitStack() as stack:
        stream = stack.enter_context(log_path.open("a", encoding="utf-8"))

        # pythonw sets these streams to None.  Redirect them before importing
        # lifecycle/tray modules so incidental prints and logging remain useful
        # and never crash the windowless process.
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream

        try:
            from work_buddy.cli.commands import launch_dashboard_app

            result = launch_dashboard_app()
            if result["ok"]:
                _write_event(stream, "OK", f"Opened {result['url']}")
                return 0
            detail = str(result.get("detail") or "work-buddy did not become ready.")
            _write_event(stream, "ERROR", detail)
        except Exception as exc:
            detail = f"Unexpected launcher error: {exc}"
            _write_event(stream, "ERROR", detail)
            traceback.print_exc(file=stream)

    _show_native_error(detail, log_path)
    return 1


if __name__ == "__main__":
    # A desktop click is not an agent-harness session. Give the launcher, and
    # any logging code it imports, a synthetic identity of its own.
    os.environ["WORK_BUDDY_SESSION_ID"] = DESKTOP_SESSION_ID
    raise SystemExit(main())
