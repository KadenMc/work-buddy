---
name: Vault Health Namespace
kind: integration
description: 'Cross-cutting concerns about the vault as a whole — reconnaissance, drift detection, hygiene checks, schema validation. First member: vault_recon (diagnostic) and the vault-recon collector (periodic discovery loop).'
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

The collector layer (`vault_recon_collector`) runs on cron, persists snapshots to `.data/vault_recon/`, computes deltas, and escalates significant changes to an investigation agent via a one-shot `type: prompt` job. The user is in the loop only at the proposal-acceptance step (via `request_send`); auto-cementing is not in scope for this round.
