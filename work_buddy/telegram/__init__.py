"""Telegram bot service for Work Buddy.

A sidecar-managed service that:
- Runs a PTB (python-telegram-bot) polling loop for user input
- Exposes a Flask HTTP API for internal notification delivery
- Implements NotificationSurface for the dispatcher

Commands: /start, /help, /capture, /remote, /status, /obs
"""
