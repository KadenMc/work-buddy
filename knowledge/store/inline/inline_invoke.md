---
name: Inline Invoke
kind: capability
description: 'Execute an inline command (menu or #wb/cmd/* tag surface).'
capability_name: inline_invoke
category: inline
op: op.wb.inline_invoke
schema_version: wb-capability/v1
parameters:
  command:
    type: string
    description: Registered command name (e.g. 'task/new')
    required: true
  surface:
    type: string
    description: '''menu'' or ''tag'''
    required: true
  payload:
    type: object
    description: Surface-specific payload (file_path, selection, cursor_line, tag, tag_line, full_text, params)
mutates_state: true
retry_policy: manual
tags:
- inline
- invoke
aliases:
- inline command
- obsidian right-click
- wb/cmd tag
- run inline handler
- invoke from note
parents:
- inline
---
