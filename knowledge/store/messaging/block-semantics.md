---
name: Messaging Block Semantics
kind: concept
description: When the messaging Stop hook blocks an agent from ending its turn, and how to clear it
summary: Only the Stop hook blocks, and only on messages that still need attention — unread ones (surface once, then release) or high/urgent ones not yet resolved. Notifications born with a terminal status never block. Clear a handled message with /tmp/wb/resolve or update_message_status.
tags:
- messaging
- stop-hook
- block
- notifications
- agent-ingest
aliases:
- why is the stop hook blocking
- message blocking my turn
- clear a blocking message
- stop hook won't let me stop
- pending message keeps blocking
- agent-ingest block
- cannot end turn pending messages
parents:
- messaging
---

The messaging hooks check for pending messages addressed to a session. Only the
**Stop** hook turns a non-empty result into a `decision:block` that prevents the
agent from ending its turn; SessionStart and UserPromptSubmit only inject context,
and PostToolUse surfaces messages mid-turn without blocking.

## What blocks the Stop

A pending message contributes to the Stop block only if it still needs attention:

- **Unread** by the recipient session → blocks once. The summary auto-marks it read
  as it renders, so the next Stop releases it ("surface once, then release").
- **High/urgent priority** and still `pending` → keeps blocking even after it has
  been read, until it is resolved. Governed by `BLOCK_UNTIL_RESOLVED_PRIORITIES` in
  `work_buddy/messaging/models.py` (`{"high", "urgent"}`; set it empty to make every
  priority surface-once).

A read, normal/low-priority message does **not** block — it surfaced once and is
done. A message in any non-`pending` status never blocks: it is excluded at the
query level and pruned on the normal TTL.

## Notifications are born resolved

Fire-and-forget notifications are created with a terminal status so they never
block. The sidecar retry sweep emits `retry_success` as `status="resolved"` (it is
purely informational); `retry_exhausted` stays `pending` at high priority so a
failed background op surfaces once and is acknowledged.

## Clearing a message that is blocking

The Stop block text surfaces the verb inline. Two equivalent ways:

- `bash /tmp/wb/resolve --id <message-id>` — the generated helper (PATCHes the
  message to `resolved`).
- `update_message_status(msg_id, "resolved")` — the capability.

`read` / `reply` do **not** clear the block: they record a read or create a new
message, neither of which changes `messages.status`. Reading a *normal* message is
enough to release it on the next Stop (the block is unread-gated), but a
*high/urgent* message must be resolved.
