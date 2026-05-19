---
name: Journal Rewrite Running Notes
kind: capability
description: 'Remove processed lines from today''s daily note. Consent-gated wrapper around ``journal_backlog.rewrite_running_notes``. Umbrella-level cleanup: typically run after all the umbrella''s groups have been routed.'
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
