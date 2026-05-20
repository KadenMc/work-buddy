---
name: Inline List Commands
kind: capability
description: List registered inline commands, optionally filtered by surface.
capability_name: inline_list_commands
category: inline
op: op.wb.inline_list_commands
schema_version: wb-capability/v1
parameters:
  surface:
    type: string
    description: 'Filter: ''menu'' or ''tag'''
tags:
- inline
- list
- commands
aliases:
- list inline commands
- inline handlers
- available wb/cmd tags
parents:
- inline
---
