---
name: Journal Rewrite Running Notes
kind: capability
description: 'Explicit legacy compatibility cleanup for an archived Markdown Running Notes thread. Disabled when Obsidian is opted out and fenced by native Journal authority.'
capability_name: journal_rewrite_running_notes
category: journal
op: op.wb.journal_rewrite_running_notes
schema_version: wb-capability/v1
parameters:
  preview:
    type: dict
    description: Output of build_rewrite_preview
    required: true
  vault_root:
    type: str
    description: Override the configured vault root
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: moderate
  regret_potential: moderate
tags:
- journal
- rewrite
- running
- notes
aliases:
- rewrite daily note
- remove processed journal lines
- clean up running notes
parents:
- journal
requires:
- obsidian
---
