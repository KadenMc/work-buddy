---
name: Journal Append To Note
kind: capability
description: Append all items in a journal-group thread as bullets to a single existing vault note. Useful for project-observation clusters.
capability_name: journal_append_to_note
category: journal
op: op.wb.journal_append_to_note
schema_version: wb-capability/v1
parameters:
  thread_id:
    type: str
    description: Group sub-thread to route
    required: true
  note_path:
    type: str
    description: Vault-relative note to append to
    required: true
  vault_root:
    type: str
    description: Override the configured vault root
    required: false
  bullet_prefix:
    type: str
    description: Bullet marker (default '- ')
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- journal
- append
- to
- note
aliases:
- append journal group to note
- log group items to project note
parents:
- journal
requires:
- obsidian
---
