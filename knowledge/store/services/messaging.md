---
name: Inter-Agent Messaging
kind: service
description: Inter-agent messaging service for cross-session communication
summary: Inter-agent messaging on port 5123. Messages checked automatically by global hooks on session start and every prompt.
ports:
- 5123
entry_points:
- work_buddy.messaging
tags:
- messaging
- inter-agent
- port-5123
aliases:
- send_message
- query_messages
- messaging service
parents:
- services
- services
---

Flask HTTP API backed by SQLite for exchanging messages between Claude Code agents across different repos. Runs on localhost:5123.

Starting: powershell.exe -Command "cd <repo-root>; conda activate work-buddy; python -m work_buddy.messaging.service"

Hooks (global, in ~/.claude/settings.json):
- SessionStart (startup/resume/compact) — shows pending messages + send/reply/resolve instructions
- UserPromptSubmit (every prompt) — shows pending messages only (no instructions, saves context)
- PostToolUse — surfaces messages mid-turn (does not block)
- Stop — blocks the agent from ending its turn while messages still need attention

Stop-hook block semantics: the Stop hook blocks only on messages that still need
attention — unread ones (which surface once, then release once auto-marked read) and
high/urgent ones not yet resolved. Notifications created with a terminal status (e.g.
the retry sweep's retry_success) never block. Clear a handled message with `bash
/tmp/wb/resolve --id <id>` or the update_message_status capability — read/reply do not
change a message's status. Full detail: the messaging/block-semantics unit.

Sending and replying: Agents in other repos send via curl POST localhost:5123/messages. Replies default to broadcast (recipient_session=NULL), visible to any session in the target project. Pass recipient_session explicitly only when targeting a specific session.

Known limitation (Claude Desktop): UserPromptSubmit hook output is injected into agent context but NOT visible in user UI. Agent sees messages; user does not. SessionStart hook output IS visible. This is a Claude Desktop behavior as of v2.1.87.
