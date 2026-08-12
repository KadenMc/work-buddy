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


def open_dashboard(target_hash: str = "", *, app: bool = False) -> dict:
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
    from work_buddy.cli.commands import dashboard_app_url, dashboard_local_url

    base = dashboard_app_url(local=True) if app else dashboard_local_url()
    identity_bootstrap = False
    launch_hash = target_hash
    if app:
        try:
            from work_buddy.dashboard.local_identity_launch import (
                bootstrap_fragment_for_dashboard,
            )

            launch_hash = bootstrap_fragment_for_dashboard(
                base,
                next_hash=target_hash,
            )
            identity_bootstrap = True
        except Exception as exc:
            # Keep the pre-existing observability UI reachable during migration.
            # New human-authority writes still fail closed without a session.
            logger.warning("Could not mint dashboard identity bootstrap: %s", exc)
    try:
        from work_buddy.collectors.chrome_collector import focus_or_create_tab

        res = focus_or_create_tab(base, target_hash=launch_hash, timeout_seconds=10)
        if res is not None:
            result = {
                "ok": True,
                "via": "extension",
                "result": res,
            }
            if app:
                result["identity_bootstrap"] = identity_bootstrap
            return result
        logger.info("focus_or_create_tab timed out; falling back to webbrowser")
    except Exception as exc:
        logger.warning("focus_or_create_tab failed (%s); falling back", exc)

    import webbrowser

    url = base + launch_hash if launch_hash else base
    webbrowser.open(url)
    # Do not return/log the bearer-bearing launch URL.  The fragment is removed
    # by React before redemption, but it is still a short-lived credential.
    result = {
        "ok": True,
        "via": "webbrowser",
        "url": base if identity_bootstrap else url,
    }
    if app:
        result["identity_bootstrap"] = identity_bootstrap
    return result
