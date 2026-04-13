"""PTB Application builder and bot state management.

Builds the python-telegram-bot Application with all handlers registered.
The BotState dataclass holds runtime state shared across handlers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from work_buddy.telegram import handlers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pending response store (in-memory, notification_id ↔ Telegram message_id)
# ---------------------------------------------------------------------------

class PendingResponseStore:
    """Maps notification ID prefixes to full IDs and Telegram message IDs.

    Used for callback_data resolution (prefix → full notification_id),
    freeform reply correlation (Telegram message_id → notification_id),
    and /reply command resolution (4-digit short_id → notification_id).
    """

    def __init__(self) -> None:
        # prefix (8 chars) → full notification_id
        self._prefix_to_id: dict[str, str] = {}
        # Telegram message_id → full notification_id
        self._msg_to_id: dict[int, str] = {}
        # 4-digit short_id → full notification_id
        self._short_id_to_full: dict[str, str] = {}

    def add(
        self,
        notification_id: str,
        telegram_message_id: int | None = None,
    ) -> None:
        prefix = notification_id[:8]
        self._prefix_to_id[prefix] = notification_id
        if telegram_message_id is not None:
            self._msg_to_id[telegram_message_id] = notification_id

    def add_short_id(self, short_id: str, notification_id: str) -> None:
        """Register a 4-digit short ID for /reply command resolution."""
        self._short_id_to_full[short_id] = notification_id

    def get_notification_id(self, prefix: str) -> str | None:
        return self._prefix_to_id.get(prefix)

    def get_by_message_id(self, msg_id: int) -> str | None:
        return self._msg_to_id.get(msg_id)

    def get_by_short_id(self, short_id: str) -> str | None:
        """Resolve a 4-digit short ID to a full notification_id."""
        return self._short_id_to_full.get(short_id)

    def get_message_id_for_notification(self, notification_id: str) -> int | None:
        """Reverse lookup: notification_id → Telegram message_id (for dismiss)."""
        for msg_id, nid in self._msg_to_id.items():
            if nid == notification_id:
                return msg_id
        return None

    def remove(self, prefix: str) -> None:
        nid = self._prefix_to_id.pop(prefix, None)
        if nid:
            # Clean up message_id mappings for this notification
            self._msg_to_id = {
                k: v for k, v in self._msg_to_id.items() if v != nid
            }
            # Clean up short_id mapping
            self._short_id_to_full = {
                k: v for k, v in self._short_id_to_full.items() if v != nid
            }


# ---------------------------------------------------------------------------
# Bot state (shared across handlers via context.bot_data["state"])
# ---------------------------------------------------------------------------

@dataclass
class BotState:
    """Runtime state shared across all Telegram handlers."""

    allowed_chat_ids: set[int] = field(default_factory=set)
    capture_config: dict[str, Any] = field(default_factory=dict)
    pending_responses: PendingResponseStore = field(
        default_factory=PendingResponseStore,
    )


# ---------------------------------------------------------------------------
# Application builder
# ---------------------------------------------------------------------------

def build_app(token: str, state: BotState) -> Application:
    """Build the PTB Application with all handlers registered.

    Args:
        token: Telegram bot API token.
        state: Shared state injected into ``context.bot_data["state"]``.

    Returns:
        Configured Application ready for ``run_polling()``.
    """
    app = Application.builder().token(token).build()

    # Inject shared state
    app.bot_data["state"] = state

    # Command handlers (order matters — first match wins)
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("slash", handlers.cmd_slash))
    app.add_handler(CommandHandler("capture", handlers.cmd_capture))
    app.add_handler(CommandHandler("remote", handlers.cmd_remote))
    app.add_handler(CommandHandler("resume", handlers.cmd_resume))
    app.add_handler(CommandHandler("status", handlers.cmd_status))
    app.add_handler(CommandHandler("dashboard", handlers.cmd_dashboard))
    app.add_handler(CommandHandler("obs", handlers.cmd_obs))
    app.add_handler(CommandHandler("reply", handlers.cmd_reply))

    # Catch-all for unrecognized /commands (must be after all CommandHandlers)
    app.add_handler(MessageHandler(
        filters.COMMAND,
        handlers.on_unknown_command,
    ))

    # Inline keyboard button presses
    app.add_handler(CallbackQueryHandler(handlers.on_button))

    # Free text (not a command) — mobile capture or freeform reply
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handlers.on_free_text,
    ))

    # Error handler
    app.add_error_handler(handlers.on_error)

    logger.info("PTB Application built with %d handlers", len(app.handlers[0]))
    return app
