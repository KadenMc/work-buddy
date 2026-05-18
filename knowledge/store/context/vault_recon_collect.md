---
name: Vault Recon Collect
kind: capability
description: 'Periodic vault reconnaissance entry point: snapshot the vault via vault_recon, append to a 60-day rolling ledger at .data/vault_recon/snapshots.json, compute deltas against prior snapshots, apply 5 curated significance rules, and write a one-shot type:prompt investigation job to .data/user_jobs/ on each rule firing (deduplicated per (rule, focus) over a 7-day window). Designed to be fired daily by sidecar_jobs/vault-recon.md.'
capability_name: vault_recon_collect
category: context
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
- context
requires:
- obsidian
- datacore
---
