---
name: Reload Skill Data
kind: skill
description: 'Data-only registry refresh: reload skill declarations + workflows from disk and rebuild the registry WITHOUT purging sys.modules. Picks up edited/added declarations (including parameter-schema changes) and new workflows whose Ops already exist — no server restart, no class-identity breakage. Does NOT pick up edited Op code or brand-new Op modules (those still need a Ctrl+R restart). This is the correct tool for data changes during dev; for code changes, restart.'
category: status
op: op.wb.reload_skill_data
schema_version: wb-skill/v1
skill_name: reload_skill_data
tags:
- status
- mcp
- registry
- reload
- data-only
aliases:
- reload declarations
- data-only reload
- refresh skills without restart
- reload registry data
- reload workflows
parents:
- status
---
