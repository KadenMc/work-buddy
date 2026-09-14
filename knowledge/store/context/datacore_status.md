---
name: Datacore Status
kind: skill
description: Check if Datacore plugin is installed, initialized, and queryable. Returns version, index revision, and object type counts.
category: context
op: op.wb.datacore_status
schema_version: wb-skill/v1
skill_name: datacore_status
tags:
- context
- datacore
- status
aliases:
- datacore ready
- datacore check
- vault index status
- is datacore running
- check vault index
- datacore plugin status
- datacore health
parents:
- context
requires:
- obsidian
- datacore
---
