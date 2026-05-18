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
---

Commands: /start (verify identity, register chat), /help, /capture <text> (append to journal Running Notes), /reply <short_id> <answer> (respond to pending request by 4-digit ID), /remote <prompt> (launch Claude Code remote session), /resume (resume existing session), /status (system health), /obs <query> (search and execute Obsidian command), /slash (list slash commands), /dashboard (return dashboard URL).

Plain text messages (no command) are treated as captures and appended to the journal.

Setup: (1) Create bot via @BotFather, (2) Set TELEGRAM_BOT_TOKEN env var, (3) Enable in config.yaml (telegram.enabled: true + sidecar.services.telegram.enabled: true), (4) Restart sidecar, (5) Send /start — first chat is auto-accepted. Chat ID is auto-persisted to .telegram_chat_id and merged with config.yaml allowed_chat_ids on startup.

Config (config.yaml telegram section): bot_token_env, allowed_chat_ids (empty = auto-accept first), enabled, capture.note (resolver or path), capture.section, capture.position.

Architecture: The bot is a surface adapter plugging into the notification infrastructure via TelegramSurface (extends NotificationSurface). Two concurrent subsystems: PTB polling loop (main thread, receives user messages) and Flask HTTP API (background thread, accepts internal notification delivery on port 5125).

Internal API (used by TelegramSurface): GET /health, POST /notifications/deliver, GET /notifications/status/<id>.

Notification delivery flow: Agent calls notification_send/request_send -> Registry creates Notification -> SurfaceDispatcher.deliver() -> TelegramSurface POSTs to localhost:5125/notifications/deliver -> Flask renders and sends via PTB -> For requests: inline keyboard buttons or reply-based text input -> User responds -> handler records StandardResponse -> callback dispatch.

Deep Links: Configure dashboard.external_url in config.yaml to enable mobile access to dashboard views from Telegram.
