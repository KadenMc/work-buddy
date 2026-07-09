"""Menu actions for the tray (no Qt imports).

Thin wrappers over the SAME pure lifecycle functions the ``wbuddy`` verbs
wrap, called in-process (no subprocess, so nothing can flash a console from
the windowless tray). The Qt layer runs these on a worker thread.
"""

from __future__ import annotations

import time

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# The Settings -> Activity sub-view (bridge sparkline + event log + notification
# log). The general state-hash format (tab + st keys), NOT the legacy `#view/`
# route which is owned by a different handler.
ACTIVITY_HASH = "#tab=settings&st=activity"


def start_sidecar() -> dict:
    from work_buddy.cli import lifecycle

    return lifecycle.start_sidecar()


def stop_sidecar() -> dict:
    from work_buddy.cli import lifecycle

    return lifecycle.stop_sidecar()


def restart_sidecar() -> dict:
    """Stop then start, mirroring ``wbuddy restart`` (commands.cmd_restart)."""
    from work_buddy.cli import lifecycle

    stop = lifecycle.stop_sidecar()
    if stop["was_running"] and not stop["stopped"]:
        return stop
    time.sleep(0.5)
    return lifecycle.start_sidecar()


def open_dashboard(target_hash: str = "") -> dict:
    """Focus an existing dashboard tab/window (or create one), smartly.

    Primary path: the Chrome extension's ``focus_or_create_tab`` (reuse the
    live tab, deep-link via ``target_hash``). Fallback when the extension is
    absent or times out: a plain ``webbrowser.open``. Never raises; returns a
    small dict describing what happened.

    NOTE: a ``target_hash`` navigates an existing tab, which could discard
    unsaved work in the dashboard. Safe today because the current dashboard has
    little unsaved state and the plain "Open dashboard" button passes no hash
    (activate-only); treating unsaved input as first-class is a React-dashboard
    concern.
    """
    from work_buddy.cli.commands import dashboard_local_url

    base = dashboard_local_url()
    try:
        from work_buddy.collectors.chrome_collector import focus_or_create_tab

        res = focus_or_create_tab(base, target_hash=target_hash, timeout_seconds=10)
        if res is not None:
            return {"ok": True, "via": "extension", "result": res}
        logger.info("focus_or_create_tab timed out; falling back to webbrowser")
    except Exception as exc:
        logger.warning("focus_or_create_tab failed (%s); falling back", exc)

    import webbrowser

    url = base + target_hash if target_hash else base
    webbrowser.open(url)
    return {"ok": True, "via": "webbrowser", "url": url}
