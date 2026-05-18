---
name: Notification Surfaces
kind: service
description: Surface details — Obsidian modals, Telegram messages, Dashboard forms
summary: Three surfaces (Obsidian 27125 / Telegram 5125 / Dashboard 5127) addressed in parallel; first response dismisses the others. Response-type rendering varies.
ports:
- 27125
- 5125
- 5127
tags:
- surfaces
- obsidian
- telegram
- dashboard
- response-types
aliases:
- notification_send
- request_send
- response types
- TTL
- first-response-wins
parents:
- notifications
- notifications
---

Three surfaces, all addressed simultaneously by default. First response wins; others auto-dismiss.

| Surface | Port | Strengths | Limitations |
|---------|------|-----------|-------------|
| **Obsidian** | 27125 | Consent modals (fast turnaround), toast notices | No generic forms (boolean/freeform/choice) — routes to dashboard |
| **Telegram** | 5125 | Mobile access, inline keyboard buttons, `/reply` command | No sliders, no custom UI. Text-based fallbacks for unsupported types |
| **Dashboard** | 5127 | Richest UI: card-styled forms, all response types, toast notifications, tab management | Must have browser open |

Callers can target specific surfaces: `surfaces: ["dashboard"]` or `surfaces: ["telegram", "obsidian"]`.

## Response types and rendering

| `response_type` | Dashboard | Telegram |
|---|---|---|
| `none` | Toast only (click to dismiss, or expand if long body) | Plain message, no buttons |
| `boolean` | Yes (green) / No (red) outlined buttons | Inline keyboard: Yes / No |
| `choice` | Labeled buttons per choice (semantic colors) | Inline keyboard: one button per choice |
| `freeform` | Textarea + Submit button | "Reply to this message" prompt |
| `range` | Slider + Submit button | Number-as-text prompt |
| `custom` | Type-specific renderer (e.g., triage clarify/review) | Text summary only |

## TTL and expiry

Notifications expire after **1 hour**, requests after **2 hours**. Expired notifications are swept lazily on `list_pending()`. The `expires_at` field is set automatically in `create_notification()`.
