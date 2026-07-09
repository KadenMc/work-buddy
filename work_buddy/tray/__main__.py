"""``python -m work_buddy.tray``: the tray entry point (login item + spawn target).

Single instance: if a live tray already owns the pid file, exit 0 quietly.
The pid file is written before the Qt loop starts and cleaned (ownership-
guarded) on the way out; ``wbuddy tray disable`` withdraws the file to ask a
running tray to quit gracefully.
"""

from __future__ import annotations

import sys


def main() -> int:
    from work_buddy import tray
    from work_buddy.tray import pidfile

    if pidfile.check_existing_tray():
        return 0  # another tray instance owns the icon
    if not tray.qt_available():
        print(
            "work-buddy tray: PySide6 is missing - install the tray extra "
            "(uv sync --extra tray)",
            file=sys.stderr,
        )
        return 1
    pidfile.write_pid_file()
    try:
        from work_buddy.tray.qt import run

        return run()
    finally:
        pidfile.cleanup_pid_file()


if __name__ == "__main__":
    sys.exit(main())
