---
name: Sending Notifications
kind: directions
description: How to send fire-and-forget notifications — parameters, surface rendering, and examples
summary: 'Use notification_send for informational messages. No response expected. Appears as: Dashboard = toast popup (click to dismiss or expand), Obsidian = Notice toast, Telegram = plain message (no buttons). Use surfaces param to target a specific surface.'
trigger: agent wants to inform the user of an event with no response expected
command: wb-notify
capabilities:
- notifications/notification_send
tags:
- notifications
- notification_send
- surfaces
- fire-and-forget
aliases:
- notification_send
- notify user
- send notification
- toast
- fire-and-forget
parents:
- notifications
---

Call: mcp__work-buddy__wb_run("notification_send", params)

Parameters:
- title (required): Short subject line
- body: Longer description
- priority: "low", "normal" (default), "high", "urgent"
- surfaces: Target specific surfaces (e.g., ["telegram"], ["dashboard"]). Default: all available

Surface rendering:
- Dashboard: toast popup (click to dismiss; click to expand if body is long)
- Obsidian: Notice toast
- Telegram: plain message (no buttons)

Example:
mcp__work-buddy__wb_run("notification_send", {
    "title": "Journal updated",
    "body": "3 log entries appended to today's journal."
})
