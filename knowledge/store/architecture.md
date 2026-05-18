---
name: Architecture & Repo Structure
kind: concept
description: Repository layout, subsystem organization, and development conventions
summary: work_buddy/ is the Python package. knowledge/store/ has workflow definitions and agent docs. Contracts live in the Obsidian vault (contracts.vault_path in config).
tags:
- architecture
- repo
- structure
- conventions
---

The work-buddy repo is organized around a Python package (work_buddy/), a unified knowledge store (knowledge/store/ — workflow definitions, capability metadata, agent documentation), metacognition patterns (metacognition/), agent session data (data/agents/, gitignored), and slash commands (.claude/commands/). Contracts live in the Obsidian vault at the path configured by contracts.vault_path in config.yaml (default: work-buddy/contracts, resolved relative to vault_root). The knowledge store is the canonical documentation source for all subsystems.
