---
name: Vault Health Namespace
kind: integration
description: Legacy vault reconnaissance documentation retained for migration evidence; automatic Datacore collection and investigation are retired.
tags:
- vault
- vault-health
- recon
- discovery
- namespace
aliases:
- vault
- vault health
- vault scope
---

Top-level scope for vault-wide concerns that aren't about any single Obsidian plugin.

Members in this round:
- `vault/recon-directions` — how to read `vault_recon` output and identify recurring conventions (state machines, tag families, path conventions).
- `vault/investigation-directions` — how the spawned investigation agent reasons over a delta and surfaces a proposal.

Future siblings (not built yet):
- vault/drift-directions — catching conventions that used to recur but stopped.
- vault/hygiene-directions — orphan pages, broken links, frontmatter schema violations.
- vault/schema-directions — tag case drift, status enum compliance.

The `vault_recon_collector` cron and its one-shot investigation escalation are
disabled. Existing snapshots may be inspected explicitly during the migration
grace period, but no native content domain depends on this Obsidian/Datacore
surface and setup must not recommend re-enabling it.
