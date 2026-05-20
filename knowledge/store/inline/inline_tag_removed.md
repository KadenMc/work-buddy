---
name: Inline Tag Removed
kind: capability
description: Cancel persistent watchers whose tag was removed from a note.
capability_name: inline_tag_removed
category: inline
op: op.wb.inline_tag_removed
schema_version: wb-capability/v1
parameters:
  file_path:
    type: string
    description: Vault-relative path
    required: true
  tag:
    type: string
    description: 'Tag that was removed (with or without #)'
    required: true
mutates_state: true
retry_policy: manual
tags:
- inline
- tag
- removed
aliases:
- cancel watcher on tag delete
- inline tag removed
- wb/cmd removed
parents:
- inline
---
