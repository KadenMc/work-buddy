---
name: Datacore Query
kind: skill
description: Explicit legacy-only Datacore query against an opted-in Obsidian vault. Native domain queries and search do not require this skill; never suggest enabling Obsidian when it is opted out.
category: context
op: op.wb.datacore_query
schema_version: wb-skill/v1
parameters:
  query:
    type: str
    description: Datacore query string (e.g. '@page and path("journal")')
    required: true
  fields:
    type: str
    description: 'Comma-separated fields to include (e.g. ''$path,$tags''). Default: all.'
    required: false
  limit:
    type: int
    description: Max results (default 50)
    required: false
skill_name: datacore_query
tags:
- context
- datacore
- query
aliases:
- query vault
- search vault structure
- find pages
- find tasks datacore
- structural vault query
- datacore search
parents:
- context
requires:
- obsidian
- datacore
---
