---
name: Mcp Registry Reload
kind: capability
description: 'HEAVY: invalidate and rebuild the capability registry. Re-probes every tool, purges work_buddy.* from sys.modules, rebuilds every capability (~6-8s). Use ONLY after code changes to existing capability callables. For transient tool-probe failures (capability stuck in disabled state), use work_buddy.recovery.recheck_disabled_capability(name) instead — it re-probes only the relevant tools with a 30s cool-down. See architecture/capability-registry.'
capability_name: mcp_registry_reload
category: status
op: op.wb.mcp_registry_reload
schema_version: wb-capability/v1
tags:
- status
- mcp
- registry
- reload
aliases:
- reload capabilities
- refresh tools
- pick up new capabilities
- reload registry
- register new functions
- hot reload MCP
- rebuild capability registry
parents:
- status
---
