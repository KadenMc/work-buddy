---
name: Vault Write At Location
kind: capability
description: Legacy compatibility insert into a section of a vault note. Disabled with the Obsidian feature; never used by native Journal or Telegram capture.
capability_name: vault_write_at_location
category: journal
op: op.wb.vault_write_at_location
schema_version: wb-capability/v1
parameters:
  content:
    type: str
    description: Text to insert
    required: true
  note:
    type: str
    description: 'Note path or resolver: ''latest_journal'' (default), ''today'', or explicit vault-relative path'
    required: false
  section:
    type: str
    description: 'Header text identifying target section (default: ''Running Notes'')'
    required: false
  position:
    type: str
    description: '''top'' (default) or ''bottom'' of section'
    required: false
  source:
    type: str
    description: 'Source metadata tag (e.g. ''telegram'') — appended as #wb/capture/<source>'
    required: false
mutates_state: true
retry_policy: verify_first
tags:
- journal
- vault
- write
- at
- location
aliases:
- write at location
- vault write
- section write
- append to section
- insert into note
- capture
parents:
- journal
requires:
- obsidian
---

This capability is retained only for explicit legacy-file work during the
archive grace period. `latest_journal` and `today` are compatibility resolvers,
not native Journal targets. Normal capture commits an exact Source and a
Journal database record.
