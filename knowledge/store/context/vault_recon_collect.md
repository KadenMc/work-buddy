---
name: Vault Recon Collect
kind: capability
description: Legacy, explicit-only Datacore vault reconnaissance entry point. Its system schedule and automatic investigation escalation are retired; never suggest it when Obsidian is opted out.
capability_name: vault_recon_collect
category: context
op: op.wb.vault_recon_collect
schema_version: wb-capability/v1
parameters:
  window_days:
    type: int
    description: Snapshot retention window (default 60).
    required: false
  skip_escalation:
    type: bool
    description: If true, evaluate rules but do not spawn investigation jobs. Useful for dry runs.
    required: false
tags:
- context
- vault
- recon
- collect
aliases:
- vault recon collect
- vault recon collector
- vault snapshot
- vault delta detection
- vault discovery loop
- periodic vault recon
parents:
- context
requires:
- obsidian
- datacore
---
