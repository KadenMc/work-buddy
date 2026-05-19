---
name: Datacore Get Page
kind: capability
description: 'Get a single vault page by path with Datacore metadata: frontmatter, sections, tags, links, timestamps.'
capability_name: datacore_get_page
category: context
op: op.wb.datacore_get_page
schema_version: wb-capability/v1
parameters:
  path:
    type: str
    description: Vault-relative path (e.g. 'journal/2026-04-09.md')
    required: true
  fields:
    type: str
    description: 'Comma-separated fields. Default: all.'
    required: false
tags:
- context
- datacore
- get
- page
aliases:
- page metadata
- vault page details
- note structure
- look up a note
- fetch note metadata
- get vault page
- what's in this note
- page frontmatter
parents:
- context
requires:
- obsidian
- datacore
---
