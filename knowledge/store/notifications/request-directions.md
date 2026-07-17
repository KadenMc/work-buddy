---
name: Sending Requests and Consent
kind: directions
description: How to request user decisions — request_send, consent_request, surface rendering, blocking vs non-blocking, and handling responses
summary: 'Use request_send for general decisions (boolean, choice, freeform, range). Use consent_request for protected operations. With timeout_seconds: blocks and returns response. Without: returns immediately, use request_poll later. Max recommended timeout: 110s. Requests get a 4-digit short ID for Telegram /reply.'
trigger: agent needs a decision, confirmation, or input from the user
command: wb-request
capabilities:
- notifications/request_send
- notifications/consent/consent_request
- notifications/request_poll
tags:
- notifications
- request_send
- consent_request
- response-types
- human-in-the-loop
- blocking
aliases:
- request_send
- consent_request
- boolean request
- choice request
- freeform request
- ask user
- user decision
parents:
- notifications
---

## request_send — general-purpose

Call: mcp__work-buddy__wb_run("request_send", params)

Parameters:
- title (required): Short subject line
- body: Longer description
- response_type (required): "boolean", "choice", "freeform", "range", or "custom"
- choices: For choice type — list of {"key": "...", "label": "..."} dicts
- number_range: For range type — {"min": 1, "max": 10, "step": 1}
- timeout_seconds: Block and poll for this many seconds (max ~110). Omit for non-blocking.
- surfaces: Target specific surfaces. Default: all available

Surface rendering:
- Dashboard: card-styled form with appropriate inputs (buttons, textarea, etc.)
- Obsidian: "Open Dashboard" toast (except consent, which uses native modals)
- Telegram: inline keyboard buttons or text prompts

Requests get a 4-digit short ID (e.g., 4920) for Telegram /reply <short_id> <answer>.

## Consent — handled automatically by the gateway

When a capability you invoke via `wb_run` hits a `@requires_consent` gate, the gateway transparently creates the notification, delivers it to surfaces, and polls for a response. Ordinary cacheable approval writes a session-scoped grant. Per-invocation exact-review approval writes no grant; it creates one fingerprint-bound ephemeral authorization for the matching immediate execution. Your `wb_run` call returns the operation's normal result on approval, or `{status: "denied"}` / `{status: "timeout"}` otherwise. An exact-review timeout is terminal: later approval or operation replay cannot authorize it, so the caller must invoke the capability again for a fresh prompt. No agent-facing capability needs to be called manually. See <<wb:notifications/consent>>.

## Handling responses

Blocking (with timeout_seconds): Call blocks until user responds or timeout. Response is in result.poll.value.

Non-blocking (no timeout_seconds): Returns immediately with notification_id. Poll later:
mcp__work-buddy__wb_run("request_poll", {"notification_id": "req_XXXXXXXX", "timeout_seconds": 60})

After timeout: Request stays pending. User can still respond on any surface. Use request_poll to check.

## Examples

Yes/No:
mcp__work-buddy__wb_run("request_send", {
    "title": "Archive completed tasks?",
    "body": "10 done tasks found.",
    "response_type": "boolean",
    "timeout_seconds": 90
})

Multiple choice:
mcp__work-buddy__wb_run("request_send", {
    "title": "Reschedule weekly review?",
    "response_type": "choice",
    "choices": [
        {"key": "today", "label": "Yes, today"},
        {"key": "tomorrow", "label": "Tomorrow"},
        {"key": "skip", "label": "Skip this week"}
    ],
    "timeout_seconds": 90
})

Freeform text:
mcp__work-buddy__wb_run("request_send", {
    "title": "What should we focus on today?",
    "body": "No contracts active.",
    "response_type": "freeform",
    "timeout_seconds": 90
})
