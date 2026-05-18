---
name: Datacore Status
kind: capability
description: Check if Datacore plugin is installed, initialized, and queryable. Returns version, index revision, and object type counts.
capability_name: datacore_status
category: context
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
- context
requires:
- obsidian
- datacore
---
