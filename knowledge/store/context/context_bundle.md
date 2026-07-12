---
name: Context Bundle
kind: capability
description: Run all (or selected) collectors and save a context bundle to disk. Use individual collectors (context_git, context_chat, etc.) when you only need one source.
capability_name: context_bundle
category: context
op: op.wb.context_bundle
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Override all time windows to N days (day-granular)
    required: false
  hours:
    type: int
    description: Override all time windows to N hours (day-granular; takes precedence over days)
    required: false
  since:
    type: str
    description: 'Exact window start: ISO datetime or relative shorthand ("18h", "2d"). The precise, authoritative window for every source (minute-level); wins over hours/days.'
    required: false
  until:
    type: str
    description: 'Exact window end: ISO datetime or relative shorthand. Defaults to now when since is given.'
    required: false
  only:
    type: str
    description: 'Comma-separated collectors (e.g. ''git,chats''). Default: all.'
    required: false
tags:
- context
- bundle
aliases:
- full context collection
- context bundle
- collect everything
- snapshot all context
parents:
- context
---
