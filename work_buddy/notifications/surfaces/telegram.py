"""Telegram notification surface.

HTTP client that talks to the Telegram bot service's internal API.
Mirrors the Obsidian bridge pattern: POST to deliver, GET to poll.

The bot service handles all Telegram-native rendering (messages,
inline keyboards, etc.) via the render module.
"""

from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from work_buddy.notifications.surfaces.base import NotificationSurface
from work_buddy.notifications.models import (
    Notification,
    ResponseType,
    StandardResponse,
)


class TelegramSurface(NotificationSurface):
    """Telegram bot service notification surface."""

    def __init__(self, base_url: str | None = None):
        if base_url is None:
            from work_buddy.config import load_config
            cfg = load_config()
            port = (
                cfg.get("sidecar", {})
                .get("services", {})
                .get("telegram", {})
                .get("port", 5125)
            )
            base_url = f"http://127.0.0.1:{port}"
        self._base_url = base_url

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def supported_response_types(self) -> set[ResponseType]:
        return {
            ResponseType.NONE,
            ResponseType.BOOLEAN,
            ResponseType.CHOICE,
            ResponseType.FREEFORM,
            ResponseType.RANGE,   # text-based fallback (reply with number)
            ResponseType.CUSTOM,  # redirect to dashboard
        }

    def is_available(self) -> bool:
        """Check if the Telegram bot service is running."""
        try:
            req = Request(f"{self._base_url}/health", method="GET")
            resp = urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("status") == "ok"
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False

    def deliver(self, notification: Notification) -> bool:
        """Deliver a notification via the Telegram bot service.

        Posts the serialized notification to the bot's internal API.
        The bot service handles rendering and sending to authorized chats.
        """
        try:
            payload = json.dumps(notification.to_dict()).encode("utf-8")
            req = Request(
                f"{self._base_url}/notifications/deliver",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urlopen(req, timeout=15)
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("delivered", False)
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False

    def dismiss(self, notification_id: str, responded_via: str = "") -> bool:
        """Edit the Telegram message to show it was handled on another surface."""
        try:
            payload = json.dumps({
                "notification_id": notification_id,
                "responded_via": responded_via,
            }).encode("utf-8")
            req = Request(
                f"{self._base_url}/notifications/dismiss",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urlopen(req, timeout=10)
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("dismissed", False)
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False

    def poll_response(self, notification_id: str) -> StandardResponse | None:
        """Check if the user has responded via Telegram.

        Polls the bot service, which in turn checks the notification store.
        """
        try:
            req = Request(
                f"{self._base_url}/notifications/status/{notification_id}",
                method="GET",
            )
            resp = urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8"))

            if data.get("status") == "responded":
                return StandardResponse(
                    response_type=ResponseType.CHOICE.value,
                    value=data.get("value"),
                    raw=data,
                    surface="telegram",
                )
            return None
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None
