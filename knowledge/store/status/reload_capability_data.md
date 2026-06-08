---
name: Reload Capability Data
kind: capability
description: 'Data-only registry refresh: reload capability declarations + workflows from disk and rebuild the registry WITHOUT purging sys.modules. Picks up edited/added declarations (including parameter-schema changes) and new workflows whose Ops already exist — no server restart, no class-identity breakage. Does NOT pick up edited Op code or brand-new Op modules (those still need a Ctrl+R restart). This is the correct tool for data changes during dev; for code changes, restart.'
capability_name: reload_capability_data
category: status
op: op.wb.reload_capability_data
schema_version: wb-capability/v1
tags:
- status
- mcp
- registry
- reload
- data-only
aliases:
- reload declarations
- data-only reload
- refresh capabilities without restart
- reload registry data
- reload workflows
parents:
- status
---
