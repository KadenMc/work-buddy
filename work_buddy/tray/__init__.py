"""System-tray process management (Qt-free).

The tray itself is a PySide6 ``QSystemTrayIcon`` app (``work_buddy.tray.qt``)
running as its OWN detached process, registered as a login item beside
``WB-Sidecar`` (see ``work_buddy.autostart``). It is deliberately not a
sidecar-supervised service: the supervisor health-checks children over an HTTP
port and a tray has none, and the tray must outlive a down sidecar so its
Start action can bring one up.

This module is the Qt-free management surface and must import WITHOUT the
``tray`` extra installed: ``wbuddy start``/``restart`` call
:func:`ensure_running` best-effort on every start, including on installs that
never opted into the tray.

Shutdown protocol: the tray owns ``runtime/tray.pid`` and re-checks ownership
on every poll tick; :func:`stop_running` withdraws that file as the graceful
quit signal (Qt then removes the icon cleanly; a force-killed tray ghosts its
icon in the Windows notification area until mouse-over), escalating to a
force-kill only when the process outlives the grace window.
"""

from __future__ import annotations

import importlib.util
import logging
import time

# A plain module logger, not logging_config.get_logger: `python -m
# work_buddy.tray` imports this package before the tray's entry point runs,
# and configuring logging here would require a session identity the tray has
# not claimed yet. Records reach work-buddy's handlers once anything has
# configured logging.
logger = logging.getLogger(__name__)


def qt_available() -> bool:
    """True when the ``tray`` extra (PySide6) is importable."""
    return importlib.util.find_spec("PySide6") is not None


def is_enabled() -> bool:
    from work_buddy.config import load_config

    return bool((load_config().get("tray") or {}).get("enabled"))


def running_pid() -> int | None:
    """Pid of a live tray process, or ``None`` (stale pid files cleaned up)."""
    from work_buddy.tray import pidfile

    return pidfile.check_existing_tray()


def ensure_running() -> dict:
    """Spawn the tray if enabled and not already running. Never raises.

    Called best-effort from ``wbuddy start``/``restart`` (mid-session
    resurrection) and from ``wbuddy tray enable``. A tray problem must never
    fail a sidecar start, so every path returns a dict:
    ``{"ok", "running", "spawned", "detail", ["pid"]}``.
    """
    try:
        if not is_enabled():
            return {
                "ok": True, "running": False, "spawned": False,
                "detail": "tray disabled (tray.enabled=false)",
            }
        existing = running_pid()
        if existing:
            return {
                "ok": True, "running": True, "spawned": False, "pid": existing,
                "detail": f"tray already running (pid={existing})",
            }
        if not qt_available():
            return {
                "ok": False, "running": False, "spawned": False,
                "detail": (
                    "tray.enabled is set but PySide6 is missing - install the "
                    "tray extra (uv sync --extra tray)"
                ),
            }
        from work_buddy.process import HostRole, start_host

        # The tray host role: the GUI image of the pinned interpreter, no
        # console, stdio detached, and no inherited agent session identity.
        start_host(HostRole.TRAY, "work_buddy.tray")
        return {"ok": True, "running": True, "spawned": True, "detail": "tray spawned"}
    except Exception as exc:  # a tray problem must never break the caller
        logger.warning("tray ensure_running failed: %s", exc)
        return {
            "ok": False, "running": False, "spawned": False,
            "detail": f"tray spawn failed: {exc}",
        }


def stop_running(*, wait_seconds: float = 8.0) -> dict:
    """Stop a running tray: withdraw its pid file (graceful), then escalate.

    The tray polls its own pid-file ownership every couple of seconds and
    quits cleanly when the file is gone, which lets Qt remove the icon. Only
    a tray that outlives the grace window gets force-killed.
    """
    try:
        from work_buddy.tray import pidfile
        from work_buddy.process import is_process_alive

        pid = pidfile.check_existing_tray()
        if not pid:
            return {"ok": True, "stopped": False, "detail": "tray not running"}
        pidfile.withdraw()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if not is_process_alive(pid):
                return {
                    "ok": True, "stopped": True, "pid": pid,
                    "detail": f"tray stopped (pid={pid})",
                }
            time.sleep(0.25)
        from work_buddy.process import terminate_tree

        terminate_tree(pid)
        time.sleep(0.5)
        alive = is_process_alive(pid)
        return {
            "ok": not alive, "stopped": not alive, "pid": pid,
            "detail": (
                f"tray force-killed (pid={pid}; its icon may linger until mouse-over)"
                if not alive
                else f"could not stop tray (pid={pid})"
            ),
        }
    except Exception as exc:
        return {"ok": False, "stopped": False, "detail": f"tray stop failed: {exc}"}
