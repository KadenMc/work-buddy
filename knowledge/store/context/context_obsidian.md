---
name: Context Obsidian
kind: capability
description: 'Obsidian vault summary: journal entries, recently modified notes'
capability_name: context_obsidian
category: context
op: op.wb.context_obsidian
schema_version: wb-capability/v1
parameters:
  journal_days:
    type: int
    description: Days of journal entries (default 7)
    required: false
  modified_days:
    type: int
    description: Days of recently modified files (default 3)
    required: false
tags:
- context
- obsidian
aliases:
- vault notes
- journal entries
- what's in obsidian
- recent notes
- daily journal
parents:
- context
requires:
- obsidian
---
