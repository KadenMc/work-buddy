---
name: Vault Event Tracking
kind: concept
description: Event-driven file change tracking for Obsidian -- replaces O(n) mtime scanning, persists in localStorage
tags:
- obsidian
- vault-events
- file-tracking
- localStorage
- ledger
aliases:
- vault events
- file tracking
- vault ledger
- modification tracking
parents:
- obsidian
- obsidian
---

Event-driven file change tracking for Obsidian, replacing O(n) mtime scanning.

## How it works

On bootstrap (idempotent, auto-called by queries):
1. Load persisted ledger from localStorage (survives Obsidian restarts)
2. Compact entries older than rolling window (default 7 days, configurable via obsidian.vault_events_window_days in config.yaml)
3. Reconcile offline changes by scanning getMarkdownFiles() and comparing mtimes
4. Register vault.on('create'|'modify'|'rename'|'delete') listeners (after onLayoutReady)

Listeners update compact per-file stats in memory, debounced to localStorage every 5 seconds.

## Data Model

Per-file entry: last (unix ms of most recent event), days ({YYYY-MM-DD: N} modify count per day within window), created (unix ms if created within window), renamedFrom (previous path if renamed).

## API

bootstrap(window_days=7) -> initialize/re-initialize ledger. get_hot_files(since_date, until_date?, limit=20, exclude_folders?) -> ranked files by modification hotness. get_recent_files(since_hours=2, limit=30, exclude_folders?) -> recently modified files. status() -> ledger stats.

## Storage

In-memory: window.__wb_vault_ledger. Persistent: localStorage["wb-vault-ledger"] (~50KB for 400 files). Rolling window configurable.

## Integration

infer_activity(deep=True) uses vault event ledger when available, falling back to mtime scanning. Hot files capability fuses vault events with KTR writing intensity.

## Caveats

- Listeners only active while Obsidian is running with work-buddy plugin
- Offline changes reconciled from mtimes on bootstrap (count=1, no frequency data)
- localStorage is per-vault in Obsidian -- multi-vault setups get separate ledgers
