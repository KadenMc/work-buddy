---
name: Journal
kind: system
description: Journal capabilities, logical-day policy, mutable notes, planning, and dashboard projections.
summary: Daily-note operations share one timezone-aware day policy; React views project provider-owned Journal state through standardized widgets.
tags:
- journal
---

The Journal system owns daily-note operations, Running Notes, planning, and their dashboard projections.

## Current contracts

- `journal/journal_state` reads the resolved Journal date, logical activity window, existence, and content.
- `journal/day-lifecycle` defines timezone-aware day boundaries, DST behavior, and safe policy transitions.
- `journal/running_notes` reads the Running Notes section; `journal/running-note-lifecycle` defines the mutable entry contract presented by the React widget.
- `journal/day_planner` and `obsidian/day-planner` own planning operations and the Obsidian plugin integration.
- `services/dashboard/react` hosts the Journal view; `services/dashboard/react/widget-platform` defines how its required and optional widgets compose.

Journal providers translate these domain contracts for UI consumers. Widgets do not become the authority for vault content, calendars, or logical-day policy.
