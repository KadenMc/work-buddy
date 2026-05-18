---
name: Datacore Query
kind: capability
description: Execute a Datacore query against the vault index. Supports @page, @section, @block, @task, @list-item, @codeblock with filters like path(), tags, childof(), parentof(). Returns serialized results.
capability_name: datacore_query
category: context
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
- context
requires:
- obsidian
- datacore
---
