"""Data models for the notification/request system.

These models define the notification hierarchy and the standardized
response schema that all surfaces must produce, regardless of their
UI capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Response types — what kind of input the user needs to provide
# ---------------------------------------------------------------------------

class ResponseType(str, Enum):
    """What kind of response a request expects.

    Surfaces declare which response types they support. If a surface
    doesn't support a type, the notification system falls back to the
    next available surface or degrades gracefully (e.g., slider -> number
    range text prompt on Telegram).
    """
    NONE = "none"              # Notification only — no response needed
    BOOLEAN = "boolean"        # Yes/No
    CHOICE = "choice"          # Pick from a list (A/B/C)
    FREEFORM = "freeform"      # Free text input
    RANGE = "range"                # Pick a number in a range (slider on rich UI, text on simple)
    CUSTOM = "custom"          # Surface-specific rendering (generative UI)


class NotificationPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class SourceType(str, Enum):
    """Who is sending the notification.

    Affects response handling: programmatic sources can't process
    freeform responses, so surfaces should constrain the UI accordingly.
    """
    AGENT = "agent"            # An interactive Claude session
    PROGRAMMATIC = "programmatic"  # Sidecar, cron job, event trigger
    SYSTEM = "system"          # Internal system notification


class NotificationStatus(str, Enum):
    PENDING = "pending"        # Created, not yet delivered to any surface
    DELIVERED = "delivered"    # Sent to at least one surface
    RESPONDED = "responded"    # User has responded
    EXPIRED = "expired"        # TTL expired before response
    CANCELLED = "cancelled"    # Withdrawn by the sender


# ---------------------------------------------------------------------------
# Choice model — for CHOICE and BOOLEAN response types
# ---------------------------------------------------------------------------

@dataclass
class Choice:
    """A single option in a choice-type request.

    Attributes:
        key: Machine-readable identifier (e.g., "a", "always", "approve").
             Used for Telegram text matching (case-insensitive).
        label: Human-readable display text (e.g., "Allow always").
        description: Optional longer explanation shown in rich UIs.
    """
    key: str
    label: str
    description: str = ""


# ---------------------------------------------------------------------------
# Standardized response — what every surface must produce
# ---------------------------------------------------------------------------

@dataclass
class StandardResponse:
    """Surface-agnostic response from the user.

    Regardless of whether the user clicked a button in Obsidian or typed
    "B" in Telegram, the response is standardized to this format.

    Attributes:
        response_type: Matches the request's ResponseType.
        value: The response value, type depends on response_type:
            - BOOLEAN: bool
            - CHOICE: str (the Choice.key)
            - FREEFORM: str (user's text)
            - NUMBER_RANGE: int or float
            - CUSTOM: dict (surface-specific structured data)
        raw: The raw surface-specific response (for debugging/audit).
        surface: Which surface collected this response.
    """
    response_type: str
    value: Any
    raw: Any = None
    surface: str = ""


# ---------------------------------------------------------------------------
# Notification — the base model
# ---------------------------------------------------------------------------

@dataclass
class Notification:
    """A message sent to the user via one or more surfaces.

    This is the base model. For notifications that need responses,
    use the request_* fields to specify response type and choices.

    Attributes:
        notification_id: Unique identifier (auto-generated).
        title: Short title / subject line.
        body: Longer description or content.
        priority: Urgency level.
        source: Who sent this (e.g., "agent:c976051d", "sidecar:cron_cleanup").
        source_type: Agent, programmatic, or system.
        tags: Optional tags for filtering/routing.

        # Request fields (populated when a response is expected)
        response_type: What kind of response is expected (NONE for notifications).
        choices: For CHOICE type — the available options.
        number_range: For NUMBER_RANGE type — (min, max, step).
        custom_template: For CUSTOM type — surface-specific rendering data.
            For Obsidian: could be JS code for a generative modal.
            For Telegram: could be a message template.

        # Callback fields (what to do when the user responds)
        callback: Capability to dispatch on response: {"capability": str, "params": dict}.
        callback_session_id: Resume this Claude Code session on response.

        # Routing
        surfaces: Target surface names, or None for all available.
        delivered_surfaces: Which surfaces successfully delivered this.

        # State
        status: Current lifecycle status.
        surface: Which surface collected the response.
        response: The user's standardized response (if any).
        created_at: ISO timestamp.
        delivered_at: ISO timestamp (when first delivered to a surface).
        responded_at: ISO timestamp (when the user responded).
        expires_at: ISO timestamp (TTL-based expiry, optional).
    """
    notification_id: str = ""
    title: str = ""
    body: str = ""
    priority: str = NotificationPriority.NORMAL.value
    source: str = ""
    source_type: str = SourceType.AGENT.value
    tags: list[str] = field(default_factory=list)

    # Request fields
    response_type: str = ResponseType.NONE.value
    choices: list[dict] = field(default_factory=list)  # serialized Choice dicts
    number_range: dict | None = None  # {"min": 1, "max": 10, "step": 1}
    custom_template: dict | None = None  # surface-specific rendering data

    # Callback fields
    callback: dict | None = None
    callback_session_id: str | None = None

    # Display hints
    expandable: bool | None = None  # None = auto-detect; True = rich view needed

    # Routing
    surfaces: list[str] | None = None  # None = all available
    delivered_surfaces: list[str] = field(default_factory=list)
    short_id: str | None = None  # 4-digit numeric ID for Telegram /reply command

    # State
    status: str = NotificationStatus.PENDING.value
    surface: str | None = None  # which surface collected the response
    response: dict | None = None  # serialized StandardResponse
    created_at: str = ""
    delivered_at: str | None = None
    responded_at: str | None = None
    expires_at: str | None = None

    def is_request(self) -> bool:
        """Whether this notification expects a response."""
        return self.response_type != ResponseType.NONE.value

    def is_expandable(self) -> bool:
        """Whether this notification warrants a rich/expanded view.

        If ``expandable`` is explicitly set by the caller, use that.
        Otherwise auto-detect: requests are always expandable (they need
        rich form rendering on the dashboard). Pure notifications default
        to non-expandable — callers should set ``expandable=True`` if
        the content is too long for a toast.

        TODO: require callers to set ``expandable`` explicitly in the
        future so auto-detection can be removed.
        """
        if self.expandable is not None:
            return self.expandable
        return self.is_request()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Notification:
        """Deserialize from a dict, ignoring unknown fields.

        Handles backward compatibility for the transports -> surfaces rename:
        old JSON files with 'transports', 'delivered_transports', 'transport'
        are mapped to the new field names.
        """
        # Migrate old field names from stored JSON
        if "transports" in data and "surfaces" not in data:
            data["surfaces"] = data.pop("transports")
        if "delivered_transports" in data and "delivered_surfaces" not in data:
            data["delivered_surfaces"] = data.pop("delivered_transports")
        if "transport" in data and "surface" not in data:
            data["surface"] = data.pop("transport")

        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)
