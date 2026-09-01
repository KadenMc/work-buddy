---
name: Architecture & Repo Structure
kind: concept
description: Repository layout, subsystem organization, and development conventions
summary: work_buddy/ is the Python package; tracked knowledge/store/ contains workflows and agent docs, while private domain content lives in registered SQLite and Co-work stores.
tags:
- architecture
- repo
- structure
- conventions
---

The Work Buddy repo is organized around the `work_buddy/` Python package,
tracked `knowledge/store/` workflow and agent documentation, private registered
data stores beneath the configured data root, and `.claude/commands/` launchers.
Journal, Contracts, Projects, Tasks, and Personal Knowledge own distinct SQLite
authorities; rich mutable bodies may bind to Co-work. The tracked knowledge
store remains the canonical documentation source for all subsystems.
