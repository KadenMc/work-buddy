---
name: Obsidian Integration
kind: integration
description: Obsidian vault integration — bridge, tasks, datacore, vault writer
summary: work-buddy integrates with Obsidian via an HTTP bridge plugin on port 27125. Subsystems include tasks, datacore, day planner, and vault events.
tags:
- obsidian
- vault
- bridge
- plugins
---

work-buddy integrates with Obsidian via an HTTP bridge plugin on port 27125. The bridge provides eval_js() for executing JavaScript inside Obsidian with access to the app object. Multiple subsystems build on this: Obsidian Tasks (read/write/intelligence), Datacore (structured vault queries), Day Planner (time-block scheduling), and vault events (rolling window file tracking). Vault semantic search runs natively, outside Obsidian — see `architecture/vault-index`.
