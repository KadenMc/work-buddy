"""Notification surface adapters.

Each surface knows how to deliver notifications and collect responses
through a specific medium (Obsidian modals, Telegram, etc.).

Surfaces declare their capabilities — which ResponseTypes they support
and how they render each one. The notification system uses these
declarations to route notifications to appropriate surfaces.
"""
