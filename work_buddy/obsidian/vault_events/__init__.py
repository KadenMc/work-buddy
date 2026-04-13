"""Vault event tracking — event-driven file change ledger for Obsidian.

Registers vault event listeners via eval_js to track create/modify/rename/delete
events in a compact per-file stats store. Replaces O(n) mtime scanning with
event-driven tracking within a configurable rolling window.
"""

from work_buddy.obsidian.vault_events.env import (
    bootstrap,
    get_hot_files,
    get_recent_files,
    status,
)

__all__ = ["bootstrap", "get_hot_files", "get_recent_files", "status"]
