"""Notifications-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op



def _register() -> None:
    """Notification and request capabilities.

    Consolidated API:
      - notification_send: fire-and-forget notification
      - request_send: create + deliver + optionally poll (one call)
      - request_poll: check/wait on an existing request
      - notification_list_pending: list all pending items

    The single agent-callable consent capability is consent_list, declared in
    knowledge/store/notifications/consent/. The grant/revoke/resolve and
    create-request Python functions in work_buddy.consent are internal —
    invoked by the sidecar router, Telegram/dashboard handlers, and gateway
    auto-consent path; not exposed as agent-callable capabilities.
    """
    import os
    import time
    from work_buddy.notifications.store import (
        create_notification as _create_notif,
        get_notification as _get_notif,
        respond_to_notification as _respond,
        mark_delivered as _mark_delivered,
        list_pending as _list_pending,
    )
    from work_buddy.notifications.models import (
        Notification, StandardResponse, ResponseType,
    )

    # MCP tool call timeout is ~120s. Document this so agents set safe values.
    _MAX_RECOMMENDED_TIMEOUT = 110  # seconds — leave buffer below MCP timeout

    # --- Helper: dispatcher (routes to all available surfaces) ---
    def _get_dispatcher():
        from work_buddy.notifications.dispatcher import SurfaceDispatcher
        return SurfaceDispatcher.from_config()

    def _deliver_to_surfaces(notification_id: str) -> tuple[bool, str]:
        """Deliver via dispatcher to all available surfaces.
        Returns (any_success, error_msg)."""
        notif = _get_notif(notification_id)
        if notif is None:
            return False, f"Notification not found: {notification_id}"
        dispatcher = _get_dispatcher()
        results = dispatcher.deliver(notif, mark_delivered_fn=_mark_delivered)
        any_ok = any(results.values())
        if not any_ok:
            failed = [k for k, v in results.items() if not v]
            if not results:
                return False, "No eligible surfaces available"
            return False, f"Delivery failed on: {', '.join(failed)}"
        return True, ""

    def _poll_surfaces(
        notification_id: str,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
    ) -> dict:
        """Poll all delivered surfaces for a response."""
        notif = _get_notif(notification_id)
        if notif is None:
            return {"status": "error", "error": f"Notification not found: {notification_id}"}
        dispatcher = _get_dispatcher()
        response = dispatcher.poll_response(
            notif,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        if response is None:
            if timeout_seconds is not None:
                return {"status": "timeout", "waited_seconds": timeout_seconds}
            return {"status": "pending"}

        # First-response-wins: dismiss on all other surfaces
        notif_fresh = _get_notif(notification_id)
        if notif_fresh and notif_fresh.delivered_surfaces:
            try:
                dispatcher.dismiss_others(
                    notification_id,
                    responding_surface=response.surface,
                    delivered_surfaces=notif_fresh.delivered_surfaces,
                )
            except Exception:
                pass  # best-effort — don't block the response

        return {
            "status": "responded",
            "value": response.value,
            "surface": response.surface,
            "raw": response.raw,
        }

    def _log_to_dashboard(notif):
        """Best-effort: log notification event to dashboard's notification log."""
        try:
            import json as _json
            from urllib.request import Request as _Req, urlopen as _urlopen
            entry = {
                "notification_id": notif.notification_id,
                "title": notif.title,
                "type": "request" if notif.is_request() else "note",
                "short_id": notif.short_id,
                "response_type": notif.response_type,
                "surfaces": notif.delivered_surfaces or [],
            }
            data = _json.dumps(entry).encode("utf-8")
            req = _Req(
                "http://127.0.0.1:5127/api/notification-log",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            _urlopen(req, timeout=3)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Capability functions
    # -----------------------------------------------------------------------

    def send_notification(
        title: str,
        body: str = "",
        priority: str = "normal",
        source: str = "agent",
        tags: list | None = None,
        surfaces: list | None = None,
        expandable: bool | None = None,
    ) -> dict:
        """Send a fire-and-forget notification (no response expected).
        Creates the record and delivers to all available surfaces.
        Optionally specify surfaces=["obsidian"] to target specific ones.
        expandable: None=auto-detect, True=rich/dashboard view, False=toast-only."""
        n = Notification(
            title=title, body=body, priority=priority,
            source=source, response_type=ResponseType.NONE.value,
            tags=tags or [],
            surfaces=surfaces,
            expandable=expandable,
        )
        created = _create_notif(n)
        nid = created.notification_id
        delivered, err = _deliver_to_surfaces(nid)
        # Re-read from store to capture updated delivered_surfaces
        fresh = _get_notif(nid) or created
        if delivered:
            _log_to_dashboard(fresh)
        result = fresh.to_dict()
        result["delivered"] = delivered
        if err:
            result["delivery_error"] = err
        return result

    def request_send(
        title: str,
        body: str = "",
        response_type: str = "choice",
        choices: list | None = None,
        number_range: dict | None = None,
        custom_template: dict | None = None,
        source: str = "agent",
        source_type: str = "agent",
        priority: str = "normal",
        callback: dict | None = None,
        callback_session_id: str | None = None,
        tags: list | None = None,
        surfaces: list | None = None,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
        expandable: bool | None = None,
    ) -> dict:
        """Create a request, deliver to all available surfaces, and optionally poll.

        Without timeout_seconds: creates + delivers, returns immediately (non-blocking).
        With timeout_seconds: creates + delivers + polls until response or timeout.
        Optionally specify surfaces=["telegram"] to target specific ones.
        expandable: None=auto-detect, True=rich/dashboard view, False=toast-only."""
        # Auto-inject session ID for AgentIngest hook delivery
        if callback_session_id is None:
            callback_session_id = os.environ.get("WORK_BUDDY_SESSION_ID")

        n = Notification(
            title=title, body=body, priority=priority,
            source=source, source_type=source_type,
            response_type=response_type,
            choices=choices or [],
            number_range=number_range,
            custom_template=custom_template,
            callback=callback,
            callback_session_id=callback_session_id,
            tags=tags or [],
            surfaces=surfaces,
            expandable=expandable,
        )
        created = _create_notif(n)
        nid = created.notification_id

        # Deliver
        delivered, err = _deliver_to_surfaces(nid)
        # Re-read from store to capture updated delivered_surfaces
        fresh = _get_notif(nid) or created
        if delivered:
            _log_to_dashboard(fresh)
        result = fresh.to_dict()
        result["delivered"] = delivered
        if err:
            result["delivery_error"] = err
            return result

        # Optionally poll
        if timeout_seconds is not None:
            poll_result = _poll_surfaces(nid, timeout_seconds, interval_seconds)
            result["poll"] = poll_result

        return result

    def request_poll(
        notification_id: str,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
    ) -> dict:
        """Check/wait for a response to a previously delivered request.

        Without timeout_seconds: single immediate check.
        With timeout_seconds: blocks and polls until response or timeout."""
        return _poll_surfaces(notification_id, timeout_seconds, interval_seconds)

    def list_pending_notifications() -> list[dict]:
        """List all pending notifications/requests."""
        return [n.to_dict() for n in _list_pending()]

    register_op("op.wb.notification_send", send_notification)
    register_op("op.wb.request_send", request_send)
    register_op("op.wb.request_poll", request_poll)
    register_op("op.wb.notification_list_pending", list_pending_notifications)


_register()
