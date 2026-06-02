---
name: Telegram Bot
kind: integration
description: Telegram bot for mobile access — commands, setup, architecture
summary: 'PTB polling + Flask API on port 5125. /reply <short_id> <answer> responds to requests. Setup: TELEGRAM_BOT_TOKEN env var + config.yaml.'
ports:
- 5125
entry_points:
- work_buddy.telegram
tags:
- telegram
- bot
- mobile
- port-5125
- sidecar
aliases:
- telegram commands
- /reply
- /capture
- TELEGRAM_BOT_TOKEN
parents:
- notifications
- notifications
dev_notes: |
  ## Capture retry wiring

  `_do_capture` (work_buddy/telegram/handlers.py) calls `write_at_location` directly — the bot is a separate sidecar process, not the `wb_run` dispatch path, so it gets no automatic gateway enqueue and `@bridge_retry` is a no-op under the PTB event loop. On a failure where `work_buddy.errors.classify_error(exc) == "transient"` (e.g. `ObsidianEditorConflict`, `ObsidianStartupRace`), it enqueues the capture for the sidecar sweep via `work_buddy.mcp_server.tools.gateway.enqueue_capability_for_retry("vault_write_at_location", {...})`, defaulting `originating_session_id` to the op record's session so the `replay_of` consent principal authorizes the replay without re-prompting. Non-transient errors fall through to the generic handler. See `architecture/retry-queue` for the seam.
---

Commands: /start (verify identity, register chat), /help, /capture <text> (append to journal Running Notes), /reply <short_id> <answer> (respond to pending request by 4-digit ID), /remote <prompt> (launch Claude Code remote session), /resume (resume existing session), /status (system health), /obs <query> (search and execute Obsidian command), /slash (list slash commands), /dashboard (return dashboard URL).

Plain text messages (no command) are treated as captures and appended to the journal.

Capture resilience: if a capture's write hits a transient failure (most commonly `editor_dirty` — the target journal is open in Obsidian with unsaved edits), the text is NOT dropped. It is queued on the retry queue and the bot replies `Attempted capture to <note> — queued …`; the sidecar sweep lands it automatically once the note is free. (See `architecture/retry-queue`.)

Setup: (1) Create bot via @BotFather, (2) Set TELEGRAM_BOT_TOKEN env var, (3) Enable in config.yaml (telegram.enabled: true + sidecar.services.telegram.enabled: true), (4) Restart sidecar, (5) Send /start — first chat is auto-accepted. Chat ID is auto-persisted to .telegram_chat_id and merged with config.yaml allowed_chat_ids on startup.

Config (config.yaml telegram section): bot_token_env, allowed_chat_ids (empty = auto-accept first), enabled, capture.note (resolver or path), capture.section, capture.position.

Architecture: The bot is a surface adapter plugging into the notification infrastructure via TelegramSurface (extends NotificationSurface). Two concurrent subsystems: PTB polling loop (main thread, receives user messages) and Flask HTTP API (background thread, accepts internal notification delivery on port 5125).

Internal API (used by TelegramSurface): GET /health, POST /notifications/deliver, GET /notifications/status/<id>.

Notification delivery flow: Agent calls notification_send/request_send -> Registry creates Notification -> SurfaceDispatcher.deliver() -> TelegramSurface POSTs to localhost:5125/notifications/deliver -> Flask renders and sends via PTB -> For requests: inline keyboard buttons or reply-based text input -> User responds -> handler records StandardResponse -> callback dispatch.

Deep Links: Configure dashboard.external_url in config.yaml to enable mobile access to dashboard views from Telegram.
