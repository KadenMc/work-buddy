"""Render Notification objects into Telegram messages + keyboards.

Converts the surface-agnostic Notification model into Telegram-native
message text and InlineKeyboardMarkup for each response type.

Callback data format: ``{notification_id_prefix}:{choice_key}``
The prefix is the first 8 chars of the notification_id, keeping the
full callback_data under Telegram's 64-byte limit.

Requests with a short_id get a prominent ``[#XXXX]`` badge and a
``/reply XXXX`` hint so users can respond via the /reply command.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from work_buddy.config import load_config
from work_buddy.notifications.models import Notification, ResponseType

_cfg = load_config()


def _dashboard_view_url(notification_id: str) -> str | None:
    """Build a dashboard view URL if an external URL is configured."""
    base = _cfg.get("dashboard", {}).get("external_url", "")
    if not base:
        return None
    return f"{base.rstrip('/')}/#view/{notification_id}"


def _notif_prefix(notification_id: str) -> str:
    """Short prefix for callback data (8 chars)."""
    return notification_id[:8]


def render_notification(notification: Notification) -> dict:
    """Render a Notification into Telegram message components.

    Returns:
        Dict with:
            - text: str — message text (MarkdownV2)
            - reply_markup: InlineKeyboardMarkup | None
            - parse_mode: str
    """
    response_type = ResponseType(notification.response_type)
    is_request = notification.is_request()
    short_id = notification.short_id

    # Build message text
    parts = []

    # Title line — requests get a [#XXXX] badge
    if notification.title:
        title_escaped = _escape_md(notification.title)
        if is_request and short_id:
            parts.append(f"\\[\\#{_escape_md(short_id)}\\] *{title_escaped}*")
        else:
            parts.append(f"*{title_escaped}*")

    if notification.body:
        parts.append(_escape_md(notification.body))

    text = "\n\n".join(parts) or "Notification"

    # Build keyboard based on response type
    markup = None
    prefix = _notif_prefix(notification.notification_id)

    if response_type == ResponseType.BOOLEAN:
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Yes", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton("No", callback_data=f"{prefix}:no"),
            ]
        ])
    elif response_type == ResponseType.CHOICE:
        buttons = []
        for choice in notification.choices:
            key = choice.get("key", "")
            label = choice.get("label", key)
            cb_data = f"{prefix}:{key}"
            # Truncate if callback_data would exceed 64 bytes
            if len(cb_data.encode("utf-8")) > 64:
                cb_data = cb_data[:64]
            buttons.append(InlineKeyboardButton(label, callback_data=cb_data))
        if buttons:
            # Compact layout: 2-3 choices on one row, 4+ stacked
            if len(buttons) <= 3:
                markup = InlineKeyboardMarkup([buttons])
            else:
                markup = InlineKeyboardMarkup([[b] for b in buttons])

    elif response_type == ResponseType.FREEFORM:
        if short_id:
            text += (
                f"\n\n_Reply to this message or use "
                f"/reply {_escape_md(short_id)} \\<your answer\\>_"
            )
        else:
            text += "\n\n_Reply to this message with your answer\\._"

    elif response_type == ResponseType.RANGE:
        # Text-based fallback (no native slider in Telegram)
        nr = notification.number_range or {}
        range_min = nr.get("min", 1)
        range_max = nr.get("max", 10)
        step = nr.get("step", 1)
        step_hint = f" \\(step: {_escape_md(str(step))}\\)" if step != 1 else ""
        if short_id:
            text += (
                f"\n\n_Reply with a number between "
                f"{_escape_md(str(range_min))} and "
                f"{_escape_md(str(range_max))}{step_hint}\\._"
                f"\n_Use /reply {_escape_md(short_id)} \\<number\\>_"
            )
        else:
            text += (
                f"\n\n_Reply with a number between "
                f"{_escape_md(str(range_min))} and "
                f"{_escape_md(str(range_max))}{step_hint}\\._"
            )

    elif response_type == ResponseType.CUSTOM:
        # Dashboard-only types — link to dashboard view
        view_url = _dashboard_view_url(notification.notification_id)
        if view_url:
            text += (
                f"\n\n[Open in dashboard]({_escape_md(view_url)})"
            )
        else:
            text += (
                f"\n\n_This request requires the dashboard\\. "
                f"Open http://127\\.0\\.0\\.1:5127 to respond\\._"
            )

    # For expandable notifications (non-CUSTOM, non-consent), add a dashboard link
    is_consent = bool((notification.custom_template or {}).get("consent_meta"))
    if response_type != ResponseType.CUSTOM and not is_consent and notification.is_expandable():
        view_url = _dashboard_view_url(notification.notification_id)
        if view_url:
            text += f"\n\n[View full details]({_escape_md(view_url)})"

    return {
        "text": text,
        "reply_markup": markup,
        "parse_mode": "MarkdownV2",
    }


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    result = []
    for ch in text:
        if ch in special:
            result.append(f"\\{ch}")
        else:
            result.append(ch)
    return "".join(result)
