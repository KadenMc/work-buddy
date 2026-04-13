"""Dashboard surface — delivers workflow views to the web dashboard.

The dashboard Flask app (port 5127) holds views in-memory. This surface
POSTs to create views and GETs to poll for responses — same HTTP pattern
as the Obsidian and Telegram surfaces.
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen
from urllib.error import URLError

from work_buddy.config import load_config
from work_buddy.notifications.models import (
    Notification,
    ResponseType,
    StandardResponse,
)
from work_buddy.notifications.surfaces.base import NotificationSurface


class DashboardSurface(NotificationSurface):
    """Delivers notifications as interactive workflow views in the dashboard."""

    def __init__(self) -> None:
        cfg = load_config()
        dashboard_cfg = cfg.get("sidecar", {}).get("services", {}).get("dashboard", {})
        port = dashboard_cfg.get("port", 5127)
        host = dashboard_cfg.get("host", "127.0.0.1")
        self._base_url = f"http://{host}:{port}"

    @property
    def name(self) -> str:
        return "dashboard"

    @property
    def supported_response_types(self) -> set[ResponseType]:
        return {
            ResponseType.NONE,
            ResponseType.BOOLEAN,
            ResponseType.CHOICE,
            ResponseType.FREEFORM,
            ResponseType.CUSTOM,
        }

    def is_available(self) -> bool:
        try:
            req = Request(f"{self._base_url}/health", method="GET")
            resp = urlopen(req, timeout=5)
            return resp.status == 200
        except (URLError, OSError, TimeoutError):
            return False

    def deliver(self, notification: Notification) -> bool:
        """POST the view to the dashboard Flask API."""
        tmpl = notification.custom_template or {}
        payload = {
            "view_id": notification.notification_id,
            "title": notification.title,
            "body": notification.body,
            "view_type": tmpl.get("type", "generic"),
            "payload": tmpl,
            "response_type": notification.response_type,
            "short_id": notification.short_id,
            "choices": notification.choices,
            "expandable": notification.is_expandable(),
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{self._base_url}/api/workflow-views",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urlopen(req, timeout=10)
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("created", False)
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False

    def dismiss(self, notification_id: str, responded_via: str = "") -> bool:
        """Dismiss the workflow view in the dashboard."""
        try:
            req = Request(
                f"{self._base_url}/api/workflow-views/{notification_id}/dismiss",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urlopen(req, timeout=10)
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("dismissed", False)
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False

    def poll_response(self, notification_id: str) -> StandardResponse | None:
        """GET the response from the dashboard Flask API."""
        try:
            req = Request(
                f"{self._base_url}/api/workflow-views/{notification_id}/response",
                method="GET",
            )
            resp = urlopen(req, timeout=5)
            result = json.loads(resp.read().decode("utf-8"))

            if result.get("status") != "responded":
                return None

            return StandardResponse(
                response_type=ResponseType.CUSTOM.value,
                value=result.get("value"),
                raw=result,
                surface="dashboard",
            )
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None
