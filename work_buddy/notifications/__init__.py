"""Notification and request system for work-buddy.

Provides a generic substrate for sending messages to users and collecting
responses, with surface-specific rendering (Obsidian modals, Telegram, etc.).

Hierarchy:
    Notification (base: message to user, may not need response)
      └── Request (subtype: expects a response)
            ├── boolean
            ├── choice (A/B/C)
            ├── freeform (text)
            ├── number_range / slider
            └── ...future types

    Surface (delivery mechanism with declared capabilities)
      ├── Obsidian (rich: modals, sliders, selects, generative UI)
      ├── Telegram (text: inline keyboards, freeform, number-as-text)
      └── Dashboard (card-styled forms, all response types)

Consent is a *consumer* of this system — it creates choice-type requests
and maps responses back to grant_consent().
"""
