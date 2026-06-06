---
name: Vault Config
kind: capability
description: Add/update ('set') or remove a vault in the semantic index's vault_index.vaults config. Writes config.local.yaml (the user-override layer); changes apply on the next vault build (the 5-min cron or a manual vault_index build) — no restart. Removing a vault deletes only its config entry; its already-indexed chunks stay searchable until an explicit prune.
capability_name: vault_config
category: context
op: op.wb.vault_config
schema_version: wb-capability/v1
mutates_state: true
consent_required: true
parameters:
  action:
    type: str
    description: "'set' (add/update a vault) or 'remove'"
    required: false
  id:
    type: str
    description: Stable vault id (no '/' or '\'; it namespaces chunk ids). The default vault's id is 'vault'.
    required: true
  path:
    type: str
    description: Absolute path to the vault root directory (required for 'set').
    required: false
  include:
    type: list
    description: "Gitignore-style include globs (default ['**/*.md'])."
    required: false
  exclude:
    type: list
    description: Gitignore-style exclude globs.
    required: false
tags:
- context
- vault
- index
- config
aliases:
- edit vault
- add vault
- remove vault
- configure vault
- set vault path
parents:
- context
---

Editable surface for the vault semantic index's roots. Pairs with `vault_index`
(build/status) and `vault_search` (query). The dashboard Settings › Embeddings sub-view
calls this when a vault row is edited. Persists to `config.local.yaml` (comments there
are not preserved — it's the machine-managed override layer). An edit takes effect on the
next build; run `vault_index` action=build to reconcile immediately. Removing a vault
leaves its chunks orphaned-but-searchable until a future prune.
