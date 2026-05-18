---
name: Vault Location Writer
kind: reference
description: Section-aware vault writing — insert content at specific locations in notes
summary: 'Configurable section-aware writing. Note resolvers: ''latest_journal'', ''today'', or explicit path. MCP capability: vault_write_at_location.'
entry_points:
- work_buddy.obsidian.vault_writer
tags:
- vault
- writer
- sections
- notes
aliases:
- vault_write_at_location
- section writer
- journal append
parents:
- obsidian
- obsidian
---

work_buddy/obsidian/vault_writer.py provides configurable section-aware vault writing.

Note resolvers: 'latest_journal' (respects day-boundary), 'today', or explicit vault-relative path.
Section finding: Matches headers (any level, ignores bold/italic formatting, partial prefix match).
MCP capability: vault_write_at_location(content, note, section, position, source)

<<wb:obsidian/bridge>>
