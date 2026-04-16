"""Obsidian notification surface.

Delivers notifications and requests to the user via the work-buddy
plugin's bridge server. Uses fire-and-forget + poll pattern:

    1. POST /notifications/show → shows modal, returns immediately
    2. GET /notifications/status/:id → poll until user responds

Supports all response types:
    - NONE: shows a Notice (toast notification)
    - BOOLEAN: modal with Yes/No buttons
    - CHOICE: modal with labeled buttons (one per choice)
    - FREEFORM: modal with textarea + Submit
    - RANGE: modal with slider + Submit
    - CUSTOM: future — generative modal from template
"""

from __future__ import annotations

import json
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from work_buddy.notifications.surfaces.base import NotificationSurface
from work_buddy.notifications.models import (
    Notification,
    ResponseType,
    StandardResponse,
)


class ObsidianSurface(NotificationSurface):
    """Obsidian bridge notification surface."""

    def __init__(self, base_url: str | None = None):
        if base_url is None:
            from work_buddy.config import load_config
            cfg = load_config()
            port = cfg.get("obsidian", {}).get("bridge_port", 27125)
            base_url = f"http://127.0.0.1:{port}"
        self._base_url = base_url

    @property
    def name(self) -> str:
        return "obsidian"

    @property
    def supported_response_types(self) -> set[ResponseType]:
        return {
            ResponseType.NONE,
            ResponseType.BOOLEAN,
            ResponseType.CHOICE,
            ResponseType.FREEFORM,
            ResponseType.RANGE,
            ResponseType.CUSTOM,
        }

    def is_available(self) -> bool:
        """Check if the Obsidian bridge is reachable."""
        try:
            req = Request(f"{self._base_url}/health", method="GET")
            resp = urlopen(req, timeout=10)
            return resp.status == 200
        except (URLError, OSError, TimeoutError):
            return False

    def _is_consent_request(self, notification: Notification) -> bool:
        """Check if a notification is a consent request (uses native modal)."""
        consent_meta = (notification.custom_template or {}).get("consent_meta")
        return bool(consent_meta)

    def deliver(self, notification: Notification) -> bool:
        """Show a notification or request modal in Obsidian.

        Routing logic (evaluated top-down, first match wins):
        1. Consent requests → native Obsidian modal (fast turnaround)
        2. Expandable (long body or request) → gateway toast with deep-link
        3. Non-expandable → simple dismiss toast (click to acknowledge)

        Fire-and-forget: returns True if the bridge accepted the request.
        The user's response (for requests) is collected via poll_response().
        """
        # Build the payload for POST /notifications/show
        payload: dict = {
            "notification_id": notification.notification_id,
            "title": notification.title,
            "body": notification.body,
            "response_type": notification.response_type,
            "priority": notification.priority,
        }

        if notification.choices:
            payload["choices"] = notification.choices

        if notification.number_range:
            payload["number_range"] = notification.number_range

        # Extract consent metadata if present (for consent-type requests)
        consent_meta = (notification.custom_template or {}).get("consent_meta")
        if consent_meta:
            payload["risk"] = consent_meta.get("risk")
            payload["operation"] = consent_meta.get("operation")
            payload["default_ttl"] = consent_meta.get("default_ttl")

        # Pass through custom_template for custom modal types (e.g., triage)
        if notification.custom_template:
            payload["custom_template"] = notification.custom_template

        # Include callback info so the plugin can dispatch on deferred response
        if notification.callback:
            payload["callback"] = notification.callback

        # Include short_id for display
        if notification.short_id:
            payload["short_id"] = notification.short_id

        # Gateway routing: consent → modal, expandable → deep-link, else → dismiss
        if not self._is_consent_request(notification):
            expandable = notification.is_expandable()
            payload["gateway"] = True
            payload["expandable"] = expandable

        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{self._base_url}/notifications/show",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urlopen(req, timeout=15)
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("shown", False)
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False

    def dismiss(self, notification_id: str, responded_via: str = "") -> bool:
        """Close the modal in Obsidian if it's still open.

        Requires the work-buddy plugin to implement
        POST /notifications/dismiss. Gracefully returns False if the
        endpoint doesn't exist yet.
        """
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
        """Check if the user has responded to a request in Obsidian.

        Returns StandardResponse if responded, None if still pending.
        The response is cleared from the plugin's memory after reading.
        """
        try:
            req = Request(
                f"{self._base_url}/notifications/status/{notification_id}",
                method="GET",
            )
            resp = urlopen(req, timeout=10)
            data = json.loads(resp.read().decode("utf-8"))

            if data.get("status") == "responded":
                return StandardResponse(
                    response_type=ResponseType.CHOICE.value,  # most common
                    value=data.get("value"),
                    raw=data,
                    surface="obsidian",
                )
            return None
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None

    def deliver_and_poll(
        self,
        notification: Notification,
        poll_timeout: int = 90,
        poll_interval: int = 3,
    ) -> StandardResponse | None:
        """Convenience: deliver + poll in one call.

        Shows the notification/request, then polls for up to poll_timeout
        seconds. Returns the response if the user acts within the window,
        or None if they don't (request stays pending for later resolution).
        """
        if not self.deliver(notification):
            return None

        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            response = self.poll_response(notification.notification_id)
            if response is not None:
                return response

        return None  # Timed out — request stays pending
