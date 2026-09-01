---
name: Vault Location Writer
kind: reference
description: Legacy section-aware vault writer retained for opt-in file compatibility and pre-seal migration work outside sealed native-domain roots.
summary: 'Opt-in legacy file writer. Post-cutover native domains never use it, and authority guards refuse writes under sealed Journal, Projects, Contracts, and Personal Knowledge roots.'
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

`work_buddy/obsidian/vault_writer.py` provides legacy section-aware file writing.

The `latest_journal` and `today` resolvers exist only for compatibility with
frozen daily files and use the configured legacy Journal directory. Native
Journal capture never calls them.
Section finding: Matches headers (any level, ignores bold/italic formatting, partial prefix match).

`write_at_location` acquires the shared native-domain root write guard on its
configured resolver target before inspecting the directory or note. Explicit
note paths must also resolve inside the configured vault. The lower-level
`vault_write` entry point applies the same guard to its explicit absolute
target. Installed, invalid, paused, or sealed authority state fails closed
with no file mutation.

The shared guard covers the configured legacy roots for Journal, Projects,
Contracts, and Personal Knowledge. Tasks are not authoritative through this
writer, but their retired Markdown tree is not one of this guard's four domain
roots. Callers must not use this generic writer for task state; frozen task
compatibility has its own native task authority boundary.

The Obsidian-gated `vault_write_at_location` capability is omitted when
Obsidian is opted out. Even when an explicit compatibility profile exposes it,
the writer may operate only outside sealed native-domain roots. It is never a
fallback for native domain writes.

<<wb:obsidian/bridge>>
