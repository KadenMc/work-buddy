---
name: Messaging Block Semantics
kind: concept
description: When the messaging Stop hook blocks an agent from ending its turn, and how to clear it
summary: The Stop hook blocks only on `actionable` messages (unread → once; high/urgent → until resolved); `acknowledgement` messages (consent echoes, fire-and-forget FYIs) never block. A sender-declared `disposition` gates block-worthiness; priority sets intensity. Clear a handled message with /tmp/wb/resolve or update_message_status.
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

## Disposition decides whether a message can block

Every message carries a sender-declared **`disposition`** (a column on the messages
table) — the first gate on block-worthiness:

- **`actionable`** — an action item the agent must see/handle. May block the Stop
  hook (subject to the read/priority rules below). This is the default.
- **`acknowledgement`** — an auto-ack of something already handled in-band (a
  consent decision echoed back for the sidecar to record, a fire-and-forget FYI).
  **Never** blocks: it is excluded from the Stop summary outright, even unread and
  high-priority. It still appears in the non-blocking SessionStart / UserPromptSubmit
  context summaries.

Senders declare disposition when they emit — the notification system marks a consent
echo `acknowledgement` but a genuine request/question answer `actionable`; the retry
sweep marks `retry_success` `acknowledgement` and `retry_exhausted` `actionable`.
When a caller omits it — including the Obsidian plugin's out-of-band `consent_grant`
POST, which cannot set it — `create_message` infers it via `_classify_disposition`
(consent subjects/tags, a `notification_response` whose body title starts "Consent:",
and terminal statuses → `acknowledgement`; everything else → `actionable`). Legacy
rows are backfilled by the same rule on migration, and a missing/NULL disposition is
treated as `actionable` so nothing silently stops blocking.

(Distinct from `agent_ingest.resolve_event`'s `disposition` argument — "ack"/"process"
— which records what the agent *did* with an ingest event; this field is the sender's
*intent*.)

## How an actionable message blocks (the read/priority axis)

Among `actionable` messages, blocking is governed by read state and priority:

- **Unread** by the recipient session → blocks once. The summary auto-marks it read
  as it renders, so the next Stop releases it ("surface once, then release").
- **High/urgent priority** and still `pending` → keeps blocking even after it has
  been read, until it is resolved. Governed by `BLOCK_UNTIL_RESOLVED_PRIORITIES` in
  `work_buddy/messaging/models.py` (`{"high", "urgent"}`; set it empty to make every
  actionable priority surface-once).

A read, normal/low-priority actionable message does **not** block — it surfaced once
and is done. Disposition and priority are orthogonal: disposition decides *whether* a
message can block at all; priority decides *how hard* an actionable one blocks.

## Born-resolved is still a non-block path

A message in any non-`pending` status never blocks: it is excluded at the query level
and pruned on the normal TTL. Fire-and-forget notifications use this — the retry sweep
emits `retry_success` as `status="resolved"` (also `acknowledgement`), so it never
enters the pending/block path at all.

## Clearing a message that is blocking

The Stop block text surfaces the verb inline. Two equivalent ways:

- `bash /tmp/wb/resolve --id <message-id>` — the generated helper (PATCHes the
  message to `resolved`).
- `update_message_status(msg_id, "resolved")` — the capability.

`read` / `reply` do **not** clear the block: they record a read or create a new
message, neither of which changes `messages.status`. Reading a *normal* message is
enough to release it on the next Stop (the block is unread-gated), but a
*high/urgent* message must be resolved.
