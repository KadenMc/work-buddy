"""Menu actions for the tray (no Qt imports).

Thin wrappers over the SAME pure lifecycle functions the ``wbuddy`` verbs
wrap, called in-process (no subprocess, so nothing can flash a console from
the windowless tray). The Qt layer runs these on a worker thread.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# The Settings -> Activity sub-view (bridge sparkline + event log + notification
# log). The general state-hash format (tab + st keys), NOT the legacy `#view/`
# route which is owned by a different handler.
ACTIVITY_HASH = "#tab=settings&st=activity"


def start_sidecar() -> dict:
    from work_buddy.cli import lifecycle

    result = lifecycle.start_sidecar()
    return _with_identity_reconnect(result)


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
    return _with_identity_reconnect(lifecycle.start_sidecar())


def _with_identity_reconnect(result: dict) -> dict:
    """Attach the truthful reconnect outcome after a deliberate tray start."""

    if not result.get("started"):
        return result
    recovered = reconnect_dashboard_identity()
    combined = dict(result)
    combined["identity_reconnect"] = recovered
    if not recovered.get("ok"):
        logger.warning("Dashboard identity reconnect failed: %s", recovered["detail"])
    return combined


def _mint_dashboard_bootstrap(
    base: str,
    *,
    next_hash: str = "",
) -> tuple[str, str | None]:
    try:
        from work_buddy.dashboard.local_identity_launch import (
            bootstrap_fragment_for_dashboard,
        )

        return bootstrap_fragment_for_dashboard(base, next_hash=next_hash), None
    except Exception as exc:
        logger.warning("Could not mint dashboard identity bootstrap: %s", exc)
        return "", (
            "Could not create a trusted local dashboard session. "
            "No unauthenticated dashboard launch was attempted."
        )


def _successful_extension_mutation(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("status") != "ok":
        return False
    details = value.get("details")
    return isinstance(details, Mapping) and not details.get("error")


def _dashboard_handoff_happened(value: object) -> bool:
    if not _successful_extension_mutation(value):
        return False
    details = value["details"]
    return (
        details.get("found") is True
        or details.get("created") is True
        or details.get("focused") is True
    )


def reconnect_dashboard_identity(*, wait_until_ready: bool = True) -> dict:
    """Deliver a trusted bootstrap to a running or newly opened React tab.

    The trusted host process, never HTTP, mints each one-use grant. It first
    asks the registered extension to update an existing app tab in place. If
    no tab exists or the extension cannot prove the handoff, the normal OS
    browser launch opens one; the operation never silently assumes provenance
    is available.
    """

    from work_buddy.cli.commands import dashboard_app_url, _wait_for_dashboard_app

    base = dashboard_app_url(local=True)
    if wait_until_ready and not _wait_for_dashboard_app(base):
        return {
            "ok": False,
            "reconnected": False,
            "detail": "Dashboard did not become ready for identity reconnect.",
        }
    launch_hash, mint_error = _mint_dashboard_bootstrap(base)
    if mint_error is not None:
        return {"ok": False, "reconnected": False, "detail": mint_error}
    try:
        from work_buddy.collectors.chrome_collector import (
            focus_existing_tab,
            focus_or_create_tab,
        )

        response = focus_existing_tab(
            base,
            target_hash=launch_hash,
            timeout_seconds=10,
        )
        if not _dashboard_handoff_happened(response):
            # An already-installed unpacked extension may still be running the
            # pre-update service worker, which reports the new mutation as
            # unknown.  Fall back to the older, already-shipped operation. It
            # now receives preserve_path end-to-end, so an existing document
            # route/query survives; if no tab exists it creates the normal app
            # window, which is preferable to silently leaving the user able to
            # edit without provenance.
            launch_hash, mint_error = _mint_dashboard_bootstrap(base)
            if mint_error is not None:
                return {"ok": False, "reconnected": False, "detail": mint_error}
            response = focus_or_create_tab(
                base,
                target_hash=launch_hash,
                preserve_path=True,
                timeout_seconds=10,
            )
    except Exception as exc:
        logger.warning("Dashboard identity handoff failed: %s", exc)
        response = None
    if not _dashboard_handoff_happened(response):
        # The extension may be absent, or an unpacked pre-update worker may
        # have performed the handoff without the new response nonce. Mint a
        # fresh one-use grant (the previous one may already be consumed) and
        # use the same OS browser launch boundary as `wbuddy launch`.
        launch_hash, mint_error = _mint_dashboard_bootstrap(base)
        if mint_error is not None:
            return {"ok": False, "reconnected": False, "detail": mint_error}
        import webbrowser

        try:
            opened = webbrowser.open(base + launch_hash)
        except Exception as exc:
            logger.warning("Trusted dashboard reconnect launch failed: %s", exc)
            opened = False
        if opened is False:
            return {
                "ok": False,
                "reconnected": False,
                "detail": (
                    "Neither the browser extension nor the OS browser accepted "
                    "the trusted identity handoff."
                ),
            }
        return {
            "ok": True,
            "reconnected": True,
            "detail": "Opened a trusted dashboard session.",
        }
    details = response["details"]
    found = details.get("found") is True or (
        details.get("created") is False and details.get("focused") is True
    )
    created = details.get("created") is True
    return {
        "ok": True,
        "reconnected": found or created,
        "detail": (
            "Reconnected the open dashboard tab."
            if found
            else "Opened a trusted dashboard session."
            if created
            else "No open dashboard tab needed reconnecting."
        ),
    }


def open_dashboard(target_hash: str = "", *, app: bool = False) -> dict:
    """Focus an existing dashboard tab/window (or create one), smartly.

    Primary path: the Chrome extension's ``focus_or_create_tab`` (reuse the
    live tab, deep-link via ``target_hash``). Fallback when the extension is
    absent or times out: a plain ``webbrowser.open``. Never raises; returns a
    small dict describing what happened.

    App launches preserve an existing React route and query while delivering
    only the one-time bootstrap fragment. Legacy dashboard deep-links retain
    their existing navigation behavior.
    """
    from work_buddy.cli.commands import dashboard_app_url, dashboard_local_url

    base = dashboard_app_url(local=True) if app else dashboard_local_url()
    identity_bootstrap = False
    launch_hash = target_hash
    if app:
        launch_hash, mint_error = _mint_dashboard_bootstrap(
            base,
            next_hash=target_hash,
        )
        if mint_error is not None:
            return {
                "ok": False,
                "identity_bootstrap": False,
                "detail": mint_error,
            }
        identity_bootstrap = True
    try:
        from work_buddy.collectors.chrome_collector import focus_or_create_tab

        res = focus_or_create_tab(
            base,
            target_hash=launch_hash,
            preserve_path=app,
            timeout_seconds=10,
        )
        if _successful_extension_mutation(res):
            result = {
                "ok": True,
                "via": "extension",
                "result": res,
            }
            if app:
                result["identity_bootstrap"] = identity_bootstrap
            return result
        logger.info(
            "focus_or_create_tab was not confirmed; falling back to webbrowser"
        )
    except Exception as exc:
        logger.warning("focus_or_create_tab failed (%s); falling back", exc)

    import webbrowser

    if app and identity_bootstrap:
        # The extension can consume a one-use grant even when its response is
        # lost or comes from a pre-correlation worker. Never replay that grant
        # through the OS-browser fallback.
        launch_hash, mint_error = _mint_dashboard_bootstrap(
            base,
            next_hash=target_hash,
        )
        if mint_error is not None:
            return {
                "ok": False,
                "identity_bootstrap": False,
                "detail": mint_error,
            }
    url = base + launch_hash if launch_hash else base
    try:
        opened = webbrowser.open(url)
    except Exception as exc:
        logger.warning("webbrowser dashboard launch failed: %s", exc)
        opened = False
    if opened is False:
        return {
            "ok": False,
            "identity_bootstrap": identity_bootstrap,
            "detail": "The browser did not accept the trusted dashboard launch.",
        }
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
