"""Abstract base for notification surfaces.

A surface is a delivery mechanism that can send notifications to the user
and (for request-type notifications) collect responses. Each surface
declares its capabilities — which response types it supports and how
it renders them.

Implementations must override:
    - name: str property
    - supported_response_types: set of ResponseType values
    - is_available() -> bool
    - deliver(notification) -> bool
    - poll_response(notification_id) -> StandardResponse | None

Optionally override:
    - supports_custom_ui -> bool (for generative modal support)
    - deliver_custom(notification, template) -> bool
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from work_buddy.notifications.models import (
    Notification,
    ResponseType,
    StandardResponse,
)


class NotificationSurface(ABC):
    """Abstract base class for notification surfaces."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Surface identifier (e.g., 'obsidian', 'telegram')."""
        ...

    @property
    @abstractmethod
    def supported_response_types(self) -> set[ResponseType]:
        """Which response types this surface can handle.

        The notification system uses this to decide which surfaces
        can deliver a given request type. For example, Telegram might
        not support CUSTOM, while Obsidian supports everything.
        """
        ...

    @property
    def supports_custom_ui(self) -> bool:
        """Whether this surface supports generative/custom UI templates.

        If True, the surface can render arbitrary UI from a template
        (e.g., agent-generated Obsidian modal JS). If False, it uses
        only built-in templates for standard response types.
        """
        return ResponseType.CUSTOM in self.supported_response_types

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the surface is currently reachable.

        For Obsidian: bridge is up. For Telegram: bot is authenticated.
        """
        ...

    @abstractmethod
    def deliver(self, notification: Notification) -> bool:
        """Deliver a notification to the user via this surface.

        For notifications (response_type=NONE): fire-and-forget.
        For requests: show the appropriate UI and store pending response.

        Returns True if delivery succeeded, False otherwise.
        """
        ...

    @abstractmethod
    def poll_response(self, notification_id: str) -> StandardResponse | None:
        """Check if the user has responded to a request.

        Returns a StandardResponse if the user has responded, or None
        if still pending. For surfaces with push support (Telegram webhooks),
        this may read from a local cache that the webhook populated.

        The response is standardized regardless of surface-specific UI:
        - BOOLEAN -> StandardResponse(value=True/False)
        - CHOICE -> StandardResponse(value="the_key")
        - FREEFORM -> StandardResponse(value="user text")
        - NUMBER_RANGE -> StandardResponse(value=7)
        - CUSTOM -> StandardResponse(value={...surface-specific...})
        """
        ...

    def dismiss(
        self,
        notification_id: str,
        responded_via: str = "",
    ) -> bool:
        """Dismiss a pending notification on this surface.

        Called by the dispatcher after another surface receives the first
        response (first-response-wins).  Default is a no-op — surfaces
        that can dismiss (edit messages, close modals) should override.

        Args:
            notification_id: The notification to dismiss.
            responded_via: Name of the surface that got the response
                (for display, e.g. "Responded on Obsidian").
        """
        return False

    def can_handle(self, notification: Notification) -> bool:
        """Whether this surface can deliver this notification.

        Checks response type support and source type constraints.
        Programmatic sources that require constrained responses may
        exclude surfaces that only support freeform.
        """
        response_type = ResponseType(notification.response_type)

        # All surfaces can handle fire-and-forget notifications
        if response_type == ResponseType.NONE:
            return True

        return response_type in self.supported_response_types
