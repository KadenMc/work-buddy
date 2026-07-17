---
name: Notification & Consent System
kind: system
description: Multi-surface notifications, requests, and consent — Obsidian, Telegram, Dashboard
summary: Real-time human-in-the-loop across Obsidian, Telegram, Dashboard; notifications + requests + consent; first-response-wins dismissal.
tags:
- notifications
- consent
- requests
- surfaces
- human-in-the-loop
---

The notification system (`work_buddy/notifications/`) enables **real-time human-in-the-loop interaction** across three surfaces: Obsidian modals, Telegram messages, and the web dashboard. This is the primary mechanism for agents to communicate with the user, collect decisions, and request consent — without the user needing to be in the same terminal session.

## When to use

- **Notify** the user of events (journal updated, task synced, build complete) — fire-and-forget (`response_type: "none"`)
- **Request a decision** (yes/no, pick from choices, freeform text input) — blocks or polls for response
- **Request consent** for protected operations — cacheable requests offer grant durations; exact-review requests offer only allow-once or deny
- **Reach the user on their phone** via Telegram when they're away from the computer

## Model

- **Notification** — a message that may not need a response (`response_type: "none"`)
- **Request** — expects a response: `boolean`, `choice`, `freeform`, `range`, or `custom`
- **Consent Request** — specialized choice request. Ordinary requests offer `always` / `temporary` / `once` / `deny`; per-invocation exact-review requests offer only `once` / `deny`

Each notification gets a unique ID (`req_XXXXXXXX`). Requests also get a 4-digit **short ID** (e.g., `4920`) for easy reference on Telegram via `/reply 4920 yes`.

## Surfaces and first-response-wins

All notifications are delivered to **all available surfaces simultaneously**. When the user responds on any one surface, the others are automatically dismissed (Obsidian modal closes, Telegram message updates to "Responded on [surface]", dashboard view removed).

See `notifications/surfaces` for surface-specific strengths, limitations, and response-type rendering.

## Using the system

**Send a notification (fire-and-forget):**

```
mcp__work-buddy__wb_run("notification_send", {
    "title": "Build complete",
    "body": "All tests passed."
})
```

**Request a decision (blocking poll):**

```
mcp__work-buddy__wb_run("request_send", {
    "title": "Archive completed tasks?",
    "body": "10 done tasks found. Move to archive?",
    "response_type": "boolean",
    "timeout_seconds": 90
})
```

**Consent for `wb_run` operations is handled by the gateway automatically** — when a `@requires_consent` gate fires inside a capability you invoke, the gateway delivers the notification and polls for the user's response. Ordinary approval writes a session-scoped grant. Per-invocation exact-review approval writes no grant; it creates a single ephemeral authorization bound to the matching immediate execution. You receive `{status: "granted"}`, `{status: "denied"}`, or `{status: "timeout"}` from your original `wb_run` call. No manual orchestration. See <<wb:notifications/consent>>.

## TTL and expiry

Notifications expire after **1 hour**, requests after **2 hours**. Expired notifications are swept lazily on `list_pending()`. The `expires_at` field is set automatically in `create_notification()`.

## Callback dispatch on response

- `callback_session_id` set → dispatched via messaging service for AgentIngest hook delivery
- `callback` set → dispatched as messaging payload for sidecar executor
- Neither → just update the record; requester polls on next check

## MCP capabilities

| Capability | Purpose |
|---|---|
| `notification_send` | Fire-and-forget notification. Optional `surfaces` param |
| `request_send` | Create + deliver a request. Optional `timeout_seconds` for blocking poll, `surfaces` for targeting |
| `request_poll` | Check/wait for response to a previously delivered request |
| `consent_request` | One-call consent flow: create + deliver + poll + auto-resolve |
| `consent_request_resolve` | Manual approve/deny for deferred consent (after timeout or late response) |
| `consent_request_list` | List pending consent requests |
| `consent_grant` | Direct grant manipulation (low-level; see `notifications/consent`) |
| `consent_revoke` | Revoke a consent grant |
| `consent_list` | List all grants with status |
| `notification_list_pending` | List all pending notifications/requests |

## Consent flow (gateway-managed)

For `wb_run` operations, consent is handled transparently by the gateway — see `notifications/consent` for the full picture. The flow below applies to manual `consent_request` calls for non-gateway operations:

1. `consent_request({operation, reason, risk, timeout_seconds: 90})` — one call.
2. Obsidian modal + Telegram message + Dashboard toast appear with consent choices.
3. User responds on any surface → ordinary grant auto-written, other surfaces dismissed, result returned.
4. If timeout → `{status: "timeout", request_id: "..."}` returned; request stays pending.
   - Agent can `request_poll` later, then `consent_request_resolve`.
   - Or user responds after timeout → callback dispatched via messaging.
