"""Telegram bot sidecar service.

Runs two concurrent subsystems:
    - PTB polling loop (main thread via asyncio) for receiving Telegram updates
    - Flask HTTP server (background thread) for:
        - GET /health — sidecar health check
        - POST /notifications/deliver — internal delivery endpoint
        - GET /notifications/status/<id> — poll for user responses

The Flask API mirrors the Obsidian bridge pattern so TelegramSurface
can use the same deliver/poll interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from work_buddy.config import load_config

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent


def _setup_file_logging() -> None:
    """Configure logging to file + stderr (inherited by sidecar)."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # File handler (persistent log)
    from work_buddy.paths import data_dir
    log_dir = data_dir("agents") / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / "telegram.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Stream handler (shows in sidecar console via inherited stderr)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


# ---------------------------------------------------------------------------
# Flask HTTP API
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)

# Shared references set during startup
_bot_app = None  # PTB Application
_bot_state = None  # BotState
_event_loop = None  # asyncio event loop running PTB


@flask_app.get("/health")
def health():
    """Sidecar health check."""
    return jsonify({"status": "ok"})


@flask_app.post("/notifications/deliver")
def deliver_notification():
    """Accept a notification for delivery to Telegram.

    Expected JSON body: serialized Notification dict (same schema as
    the notification store).

    Sends the notification to all authorized chats and tracks pending
    responses for request-type notifications.
    """
    global _bot_app, _bot_state, _event_loop

    if _bot_app is None or _event_loop is None:
        return jsonify({"delivered": False, "error": "Bot not initialized"}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"delivered": False, "error": "No JSON body"}), 400

    notification_id = data.get("notification_id", "")
    if not notification_id:
        return jsonify({"delivered": False, "error": "Missing notification_id"}), 400

    # Render the notification
    from work_buddy.notifications.models import Notification
    from work_buddy.telegram.render import render_notification

    notif = Notification.from_dict(data)
    rendered = render_notification(notif)

    # Send to all authorized chats
    sent_to = []
    errors = []

    for chat_id in _bot_state.allowed_chat_ids:
        try:
            future = asyncio.run_coroutine_threadsafe(
                _bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=rendered["text"],
                    reply_markup=rendered.get("reply_markup"),
                    parse_mode=rendered.get("parse_mode"),
                ),
                _event_loop,
            )
            message = future.result(timeout=15)

            # Track for response correlation
            _bot_state.pending_responses.add(
                notification_id,
                telegram_message_id=message.message_id,
            )
            # Register short_id for /reply command
            if notif.short_id:
                _bot_state.pending_responses.add_short_id(
                    notif.short_id, notification_id,
                )
            sent_to.append(chat_id)
        except Exception as exc:
            logger.error("Failed to send to chat %s: %s", chat_id, exc)
            errors.append({"chat_id": chat_id, "error": str(exc)})

    delivered = len(sent_to) > 0
    result = {
        "delivered": delivered,
        "notification_id": notification_id,
        "sent_to_chats": len(sent_to),
    }
    if errors:
        result["errors"] = errors

    return jsonify(result), 200 if delivered else 500


@flask_app.post("/notifications/dismiss")
def dismiss_notification():
    """Edit the Telegram message to show the request was handled elsewhere.

    Called by TelegramSurface.dismiss() when another surface
    (Obsidian, Dashboard) receives the first response.
    """
    global _bot_app, _bot_state, _event_loop

    if _bot_app is None or _event_loop is None:
        return jsonify({"dismissed": False, "error": "Bot not initialized"}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"dismissed": False, "error": "No JSON body"}), 400

    notification_id = data.get("notification_id", "")
    responded_via = data.get("responded_via", "another device")

    if not notification_id:
        return jsonify({"dismissed": False, "error": "Missing notification_id"}), 400

    # Find the Telegram message for this notification
    msg_id = _bot_state.pending_responses.get_message_id_for_notification(
        notification_id,
    )
    if msg_id is None:
        # Try prefix lookup
        prefix = notification_id[:8]
        full_id = _bot_state.pending_responses.get_notification_id(prefix)
        if full_id:
            msg_id = _bot_state.pending_responses.get_message_id_for_notification(
                full_id,
            )

    if msg_id is None:
        return jsonify({"dismissed": False, "reason": "not_in_pending_store"})

    # Get the notification title for a human-readable dismiss message
    title = notification_id  # fallback
    try:
        from work_buddy.notifications.store import get_notification
        notif = get_notification(notification_id)
        if notif:
            title = notif.title
    except Exception:
        pass

    # Edit the message across all chats
    dismissed_any = False
    for chat_id in _bot_state.allowed_chat_ids:
        try:
            dismiss_text = f"Responded to \"{title}\" on {responded_via}."
            future = asyncio.run_coroutine_threadsafe(
                _bot_app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=dismiss_text,
                ),
                _event_loop,
            )
            future.result(timeout=10)
            dismissed_any = True
        except Exception as exc:
            logger.debug("Could not dismiss in chat %s: %s", chat_id, exc)

    # Clean up pending store
    if dismissed_any:
        prefix = notification_id[:8]
        _bot_state.pending_responses.remove(prefix)

    return jsonify({"dismissed": dismissed_any})


@flask_app.get("/notifications/status/<notification_id>")
def get_notification_status(notification_id: str):
    """Poll for a user response to a notification.

    Checks the notification store for a response. If the user has
    responded via Telegram (button or reply), it will be recorded there.
    """
    try:
        from work_buddy.notifications.store import get_notification
        from work_buddy.notifications.models import NotificationStatus

        notif = get_notification(notification_id)
        if notif is None:
            return jsonify({"status": "not_found"}), 404

        if notif.status == NotificationStatus.RESPONDED.value and notif.response:
            return jsonify({
                "status": "responded",
                "value": notif.response.get("value"),
                "surface": notif.response.get("surface", "telegram"),
            })

        return jsonify({"status": "pending"})
    except Exception as exc:
        logger.error("Status check failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Service startup
# ---------------------------------------------------------------------------

def _run_flask(port: int) -> None:
    """Run Flask in a background thread."""
    flask_app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def main() -> None:
    """Entry point: start Flask thread + PTB polling loop."""
    global _bot_app, _bot_state, _event_loop

    import uuid

    # Synthetic session ID so consent system and logging can initialize.
    # Sidecar services don't have agent sessions — this follows the
    # same pattern as the embedding service (__main__.py).
    if not os.environ.get("WORK_BUDDY_SESSION_ID"):
        os.environ["WORK_BUDDY_SESSION_ID"] = f"telegram-{uuid.uuid4().hex[:8]}"

    # Load .env file so TELEGRAM_BOT_TOKEN is available
    # (sidecar spawns us as a subprocess without .env loading)
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path(__file__).parent.parent.parent / ".env"
        load_dotenv(env_path)
    except ImportError:
        pass

    # Set up file logging (sidecar pipes stdout/stderr to DEVNULL)
    _setup_file_logging()

    cfg = load_config()
    telegram_cfg = cfg.get("telegram", {})

    # Read bot token from environment variable
    token_env = telegram_cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(token_env, "")
    if not token:
        logger.error(
            "Telegram bot token not found in env var '%s'. "
            "Set it and restart the service.", token_env,
        )
        raise SystemExit(1)

    # Single-instance guard: if another telegram service is already serving
    # the Flask port, exit immediately.  Two PTB processes against the same
    # bot token cause getUpdates to alternate between them, stranding button
    # responses in whichever in-memory PendingResponseStore didn't deliver.
    sidecar_cfg = cfg.get("sidecar", {}).get("services", {}).get("telegram", {})
    flask_port = sidecar_cfg.get("port", 5125)
    try:
        from urllib.request import Request, urlopen
        probe = Request(f"http://127.0.0.1:{flask_port}/health", method="GET")
        resp = urlopen(probe, timeout=2)
        if resp.status == 200:
            logger.error(
                "Another telegram service is already running on port %d. "
                "Refusing to start a second instance.", flask_port,
            )
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception:
        pass  # No existing instance — proceed.

    # Build bot state
    from work_buddy.telegram.bot import BotState, build_app

    from work_buddy.telegram.handlers import load_persisted_chat_ids

    allowed_ids = set(telegram_cfg.get("allowed_chat_ids", []))
    persisted_ids = load_persisted_chat_ids()
    if persisted_ids:
        allowed_ids |= persisted_ids
        logger.info("Loaded %d persisted chat ID(s)", len(persisted_ids))
    capture_cfg = telegram_cfg.get("capture", {
        "note": "latest_journal",
        "section": "Running Notes",
        "position": "top",
    })

    _bot_state = BotState(
        allowed_chat_ids=allowed_ids,
        capture_config=capture_cfg,
    )

    _bot_app = build_app(token, _bot_state)

    # Rebuild pending-response prefix mappings from the on-disk store.
    # PendingResponseStore is in-memory, so a clean restart would otherwise
    # strand any consent buttons sent by the previous instance.
    try:
        from work_buddy.notifications.store import list_pending
        rebuilt = 0
        for n in list_pending():
            if "telegram" in (n.delivered_surfaces or []):
                _bot_state.pending_responses.add(n.notification_id)
                if n.short_id:
                    _bot_state.pending_responses.add_short_id(
                        n.short_id, n.notification_id,
                    )
                rebuilt += 1
        if rebuilt:
            logger.info(
                "Rebuilt %d pending-response mapping(s) from store", rebuilt,
            )
    except Exception as exc:
        logger.warning("Failed to rebuild pending responses: %s", exc)

    # Start Flask health/delivery API in background thread
    flask_thread = threading.Thread(
        target=_run_flask,
        args=(flask_port,),
        daemon=True,
        name="telegram-flask",
    )
    flask_thread.start()
    logger.info("Flask HTTP API started on port %d", flask_port)

    # Capture the event loop for cross-thread message sending
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _event_loop = loop

    # Run PTB polling (blocks until stopped)
    logger.info("Starting Telegram bot polling...")
    _bot_app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
