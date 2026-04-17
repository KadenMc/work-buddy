"""Multi-surface notification dispatcher.

Routes notifications to one or more surfaces (Obsidian, Telegram, etc.)
based on availability, capability, and caller preferences.

Default policy: deliver to ALL available surfaces. For requests, the
first response from any surface wins.

Callers can optionally target specific surfaces via the ``surfaces``
field on the Notification model or the ``surfaces`` parameter on
MCP capabilities like ``notification_send`` and ``request_send``.
"""

from __future__ import annotations

import time
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.notifications.models import (
    Notification,
    StandardResponse,
)
from work_buddy.notifications.surfaces.base import NotificationSurface

logger = get_logger(__name__)


class SurfaceDispatcher:
    """Routes notifications to registered surfaces."""

    def __init__(self) -> None:
        self._surfaces: list[NotificationSurface] = []

    @classmethod
    def from_config(cls) -> SurfaceDispatcher:
        """Build a dispatcher with all configured surfaces.

        Imports surfaces lazily so unavailable services don't block startup.
        """
        dispatcher = cls()

        # Always register Obsidian (core surface)
        try:
            from work_buddy.notifications.surfaces.obsidian import ObsidianSurface
            dispatcher.register(ObsidianSurface())
        except Exception as exc:
            logger.warning("Failed to initialize ObsidianSurface: %s", exc)

        # Always register Dashboard (core surface)
        try:
            from work_buddy.notifications.surfaces.dashboard import DashboardSurface
            dispatcher.register(DashboardSurface())
        except Exception as exc:
            logger.debug("Dashboard surface not available: %s", exc)

        # Register Telegram if configured + enabled + user hasn't opted out
        try:
            from work_buddy.config import load_config
            cfg = load_config()
            telegram_enabled = cfg.get("telegram", {}).get("enabled", False)
            # Honor user preference: features.telegram.wanted=false means skip
            wanted = cfg.get("features", {}).get("telegram", {}).get("wanted", True)
            if telegram_enabled and wanted is not False:
                from work_buddy.notifications.surfaces.telegram import TelegramSurface
                dispatcher.register(TelegramSurface())
            elif telegram_enabled and wanted is False:
                logger.debug("Telegram surface skipped: features.telegram.wanted=false")
        except Exception as exc:
            logger.debug("Telegram surface not available: %s", exc)

        return dispatcher

    def register(self, surface: NotificationSurface) -> None:
        """Add a surface to the dispatcher."""
        self._surfaces.append(surface)
        logger.debug("Registered surface: %s", surface.name)

    @property
    def surface_names(self) -> list[str]:
        """Names of all registered surfaces."""
        return [s.name for s in self._surfaces]

    def _select_surfaces(
        self,
        notification: Notification,
    ) -> list[NotificationSurface]:
        """Select surfaces for a notification based on routing preferences.

        Order: requested surfaces -> all available that can handle it.
        """
        requested = notification.surfaces  # None = all available
        candidates = []

        for s in self._surfaces:
            # Skip if caller specified surfaces and this one isn't in the list
            if requested and s.name not in requested:
                continue
            # Skip if surface can't handle this response type
            if not s.can_handle(notification):
                continue
            candidates.append(s)

        return candidates

    def deliver(
        self,
        notification: Notification,
        mark_delivered_fn: Any = None,
    ) -> dict[str, bool]:
        """Deliver a notification to all eligible surfaces.

        Args:
            notification: The notification to deliver.
            mark_delivered_fn: Optional callback ``(notification_id, surface_name) -> ...``
                to record delivery in the store.

        Returns:
            Dict mapping surface name -> success bool.
        """
        candidates = self._select_surfaces(notification)
        if not candidates:
            logger.warning(
                "No eligible surfaces for notification %s (requested=%s)",
                notification.notification_id, notification.surfaces,
            )
            return {}

        results: dict[str, bool] = {}
        for s in candidates:
            if not s.is_available():
                logger.debug("Surface %s not available, skipping", s.name)
                results[s.name] = False
                continue

            try:
                ok = s.deliver(notification)
                results[s.name] = ok
                if ok and mark_delivered_fn:
                    mark_delivered_fn(notification.notification_id, s.name)
                if ok:
                    logger.info(
                        "Delivered %s via %s",
                        notification.notification_id, s.name,
                    )
                else:
                    logger.warning(
                        "Delivery failed for %s via %s",
                        notification.notification_id, s.name,
                    )
            except Exception as exc:
                logger.error(
                    "Surface %s raised during delivery: %s", s.name, exc,
                )
                results[s.name] = False

        return results

    def dismiss_others(
        self,
        notification_id: str,
        responding_surface: str,
        delivered_surfaces: list[str] | None = None,
    ) -> dict[str, bool]:
        """Dismiss the notification on all surfaces except the responding one.

        Called after a response is received on one surface to update/close
        the notification on all others (first-response-wins).

        Args:
            notification_id: The notification that was responded to.
            responding_surface: Name of the surface that got the response.
            delivered_surfaces: List of surfaces that delivered this
                notification. If None, tries all registered surfaces.

        Returns:
            Dict mapping surface name -> success bool.
        """
        results: dict[str, bool] = {}
        for s in self._surfaces:
            if s.name == responding_surface:
                continue
            if delivered_surfaces and s.name not in delivered_surfaces:
                continue
            try:
                ok = s.dismiss(notification_id, responded_via=responding_surface)
                results[s.name] = ok
                if ok:
                    logger.info(
                        "Dismissed %s on %s (responded via %s)",
                        notification_id, s.name, responding_surface,
                    )
            except Exception as exc:
                logger.warning(
                    "Dismiss failed on %s for %s: %s",
                    s.name, notification_id, exc,
                )
                results[s.name] = False
        return results

    def poll_response(
        self,
        notification: Notification,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
    ) -> StandardResponse | None:
        """Poll all delivered surfaces for a response.

        Without timeout: single immediate check across all delivered surfaces.
        With timeout: blocks and polls until response or timeout.
        First response from any surface wins.
        """
        delivered = notification.delivered_surfaces or []
        if not delivered:
            # Fall back to all surfaces that can handle it
            delivered = [s.name for s in self._select_surfaces(notification)]

        active_surfaces = [
            s for s in self._surfaces
            if s.name in delivered
        ]

        if not active_surfaces:
            return None

        if timeout_seconds is None:
            # Single check
            for s in active_surfaces:
                try:
                    resp = s.poll_response(notification.notification_id)
                    if resp is not None:
                        return resp
                except Exception as exc:
                    logger.debug("Poll failed on %s: %s", s.name, exc)
            return None

        # Blocking poll with timeout
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            for s in active_surfaces:
                try:
                    resp = s.poll_response(notification.notification_id)
                    if resp is not None:
                        return resp
                except Exception as exc:
                    logger.debug("Poll failed on %s: %s", s.name, exc)
            time.sleep(interval_seconds)

        return None
